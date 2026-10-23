"""Judging a reported position against the offices a person may clock at, as pure functions
(ADR-031; docs/database-design.md §4.5, location verification).

Every threshold comes from the office's check (radius, accepted accuracy, position age); none is
assumed here (ADR-026). The mechanism:

- Distance is the great-circle (haversine) distance on a sphere of the mean Earth radius; at
  office scale its error is far below a browser's accuracy.
- Per office: a position older than the check's maximum age is `unverified` (`stale_position`);
  a position whose reported accuracy is worse than the accepted accuracy is `inconclusive`
  (`low_accuracy`); otherwise it is `verified` within the radius, `outside_radius` when even the
  near edge of the accuracy circle is beyond the radius, and `inconclusive` (`boundary`) when the
  circle straddles it, so an uncertain position is never called a definite mismatch.
- Across offices: the best result wins (`verified`, then `inconclusive`, then `outside_radius`,
  then `unverified`); among equals the nearest office relative to its radius, then the person's
  placed office, then the office code, then the check ID. One office is chosen, so one evidence
  record names one configuration.
- A failure reported by the browser (denied, unavailable, timed out, unsupported, skipped) or no
  report at all is `unverified` against the preferred office.
- Position age is measured on the device's own clock (sent minus captured), so a wrong device
  clock does not make every position stale; a capture after sending is `invalid_timestamp`.
  Nothing the device reports is proof of where anyone was.

Stored values are reduced: distance to 10 m (capped at 10 km) and accuracy rounded up to 5 m.
"""

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

EARTH_RADIUS_M: Final = 6_371_008.8
DISTANCE_STEP_M: Final = 10
DISTANCE_CAP_M: Final = 10_000
ACCURACY_STEP_M: Final = 5
ACCURACY_CAP_M: Final = 10_000
# A capture reported this far after sending is a device clock fault, not a fresh position.
CAPTURE_AFTER_SEND_TOLERANCE_SECONDS: Final = 5


class Outcome(StrEnum):
    VERIFIED = "verified"
    OUTSIDE_RADIUS = "outside_radius"
    INCONCLUSIVE = "inconclusive"
    UNVERIFIED = "unverified"


class Reason(StrEnum):
    # Inconclusive.
    LOW_ACCURACY = "low_accuracy"
    BOUNDARY = "boundary"
    # Unverified: reported by the browser.
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"
    # Unverified: decided here.
    NOT_PROVIDED = "not_provided"
    STALE_POSITION = "stale_position"
    INVALID_TIMESTAMP = "invalid_timestamp"


# What the browser may report instead of a position.
BROWSER_FAILURES: Final = frozenset(
    {Reason.PERMISSION_DENIED, Reason.UNAVAILABLE, Reason.TIMEOUT, Reason.UNSUPPORTED, Reason.SKIPPED}
)
_RANK: Final = {
    Outcome.VERIFIED: 0,
    Outcome.INCONCLUSIVE: 1,
    Outcome.OUTSIDE_RADIUS: 2,
    Outcome.UNVERIFIED: 3,
}


@dataclass(frozen=True, slots=True)
class Office:
    """One office the person may clock at on the work date, with its check in effect."""

    check_id: uuid.UUID
    location_id: uuid.UUID
    code: str
    latitude: float
    longitude: float
    radius_m: int
    max_accuracy_m: int
    max_position_age_seconds: int
    review_unverified: bool
    # The office of the person's job on the work date.
    placed: bool


@dataclass(frozen=True, slots=True)
class Position:
    latitude: float
    longitude: float
    accuracy_m: float
    # Both on the device's clock.
    captured_at: datetime
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class Verdict:
    office: Office
    outcome: Outcome
    reasons: tuple[Reason, ...]
    distance_m: int | None
    distance_over_cap: bool
    accuracy_m: int | None
    position_age_seconds: int | None
    review_required: bool
    # Unrounded, for choosing between offices only; never stored.
    margin_m: float


def distance_m(latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float) -> float:
    phi_1, phi_2 = math.radians(latitude_1), math.radians(latitude_2)
    d_phi = phi_2 - phi_1
    d_lambda = math.radians(longitude_2 - longitude_1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def stored_distance(raw: float) -> tuple[int, bool]:
    """The distance as stored: to the nearest 10 m, at most 10 km (and whether it was capped)."""
    if raw > DISTANCE_CAP_M:
        return DISTANCE_CAP_M, True
    return math.floor(raw / DISTANCE_STEP_M + 0.5) * DISTANCE_STEP_M, False


def stored_accuracy(raw: float) -> int:
    """The accuracy as stored: rounded up to 5 m, at most 10 km (never better than reported)."""
    return min(ACCURACY_CAP_M, math.ceil(raw / ACCURACY_STEP_M) * ACCURACY_STEP_M)


@dataclass(frozen=True, slots=True)
class _Measured:
    distance_m: int | None = None
    distance_over_cap: bool = False
    accuracy_m: int | None = None
    position_age_seconds: int | None = None
    margin_m: float = 0.0


def _verdict(
    office: Office, outcome: Outcome, reasons: tuple[Reason, ...], measured: _Measured | None = None
) -> Verdict:
    taken = measured or _Measured()
    review = outcome is not Outcome.VERIFIED and (
        outcome is not Outcome.UNVERIFIED or office.review_unverified
    )
    return Verdict(
        office=office,
        outcome=outcome,
        reasons=reasons,
        distance_m=taken.distance_m,
        distance_over_cap=taken.distance_over_cap,
        accuracy_m=taken.accuracy_m,
        position_age_seconds=taken.position_age_seconds,
        review_required=review,
        margin_m=taken.margin_m,
    )


def judge_office(office: Office, position: Position) -> Verdict:
    age = (position.sent_at - position.captured_at).total_seconds()
    if age < -CAPTURE_AFTER_SEND_TOLERANCE_SECONDS:
        return _verdict(office, Outcome.UNVERIFIED, (Reason.INVALID_TIMESTAMP,))
    age_seconds = max(0, int(age))
    if age > office.max_position_age_seconds:
        return _verdict(
            office, Outcome.UNVERIFIED, (Reason.STALE_POSITION,), _Measured(position_age_seconds=age_seconds)
        )
    raw = distance_m(position.latitude, position.longitude, office.latitude, office.longitude)
    distance, capped = stored_distance(raw)
    measured = _Measured(
        distance_m=distance,
        distance_over_cap=capped,
        accuracy_m=stored_accuracy(position.accuracy_m),
        position_age_seconds=age_seconds,
        margin_m=raw - office.radius_m,
    )
    if position.accuracy_m > office.max_accuracy_m:
        return _verdict(office, Outcome.INCONCLUSIVE, (Reason.LOW_ACCURACY,), measured)
    if raw <= office.radius_m:
        return _verdict(office, Outcome.VERIFIED, (), measured)
    if raw - position.accuracy_m > office.radius_m:
        return _verdict(office, Outcome.OUTSIDE_RADIUS, (), measured)
    return _verdict(office, Outcome.INCONCLUSIVE, (Reason.BOUNDARY,), measured)


def _ordered(offices: Sequence[Office]) -> list[Office]:
    return sorted(
        offices, key=lambda office: (not office.placed, office.code.casefold(), str(office.check_id))
    )


def judge(offices: Sequence[Office], report: Position | Reason | None) -> Verdict | None:
    """The verdict to store for one clock event, or None when no office the person may clock
    at has a check (nothing is stored then). `report` is the position, the browser's failure,
    or None when the client sent nothing."""
    if not offices:
        return None
    ordered = _ordered(offices)
    if not isinstance(report, Position):
        reason = report if report in BROWSER_FAILURES else Reason.NOT_PROVIDED
        return _verdict(ordered[0], Outcome.UNVERIFIED, (reason,))
    verdicts = [judge_office(office, report) for office in ordered]
    return min(
        enumerate(verdicts),
        key=lambda item: (_RANK[item[1].outcome], item[1].margin_m, item[0]),
    )[1]
