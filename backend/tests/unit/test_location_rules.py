"""Judging a reported position against offices (ADR-031), as pure functions. Coordinates and
limits are obviously fake test values (docs/database-design.md §10)."""

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.attendance.location_rules import (
    DISTANCE_CAP_M,
    Office,
    Outcome,
    Position,
    Reason,
    distance_m,
    judge,
    judge_office,
    stored_accuracy,
    stored_distance,
)

SENT = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
# Metres per degree of latitude on the rules' sphere.
M_PER_DEGREE = math.radians(1) * 6_371_008.8


def office(
    *,
    code: str = "TEST-A",
    latitude: float = 12.0,
    longitude: float = 77.0,
    radius_m: int = 150,
    max_accuracy_m: int = 100,
    max_age: int = 120,
    review_unverified: bool = True,
    placed: bool = False,
) -> Office:
    return Office(
        check_id=uuid.uuid4(),
        location_id=uuid.uuid4(),
        code=code,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        max_accuracy_m=max_accuracy_m,
        max_position_age_seconds=max_age,
        review_unverified=review_unverified,
        placed=placed,
    )


def north_of(place: Office, metres: float, *, accuracy: float = 10, age: float = 5) -> Position:
    """A position due north of an office, `metres` away along the meridian."""
    return Position(
        latitude=place.latitude + metres / M_PER_DEGREE,
        longitude=place.longitude,
        accuracy_m=accuracy,
        captured_at=SENT - timedelta(seconds=age),
        sent_at=SENT,
    )


def test_distance_along_a_meridian_and_across_the_globe() -> None:
    assert distance_m(12.0, 77.0, 12.0, 77.0) == 0
    assert distance_m(12.0, 77.0, 12.0 + 1000 / M_PER_DEGREE, 77.0) == pytest.approx(1000, abs=0.01)
    # Half the circumference between antipodes.
    assert distance_m(0.0, 0.0, 0.0, 180.0) == pytest.approx(math.pi * 6_371_008.8, rel=1e-9)


@pytest.mark.parametrize(
    ("metres", "accuracy", "outcome", "reasons"),
    [
        (0, 10, Outcome.VERIFIED, ()),
        (149.9, 10, Outcome.VERIFIED, ()),
        (150.0, 10, Outcome.VERIFIED, ()),  # on the radius counts as inside
        (150.1, 10, Outcome.INCONCLUSIVE, (Reason.BOUNDARY,)),  # the circle reaches inside
        (160.0, 10, Outcome.INCONCLUSIVE, (Reason.BOUNDARY,)),  # near edge exactly on the radius
        (160.1, 10, Outcome.OUTSIDE_RADIUS, ()),  # even the near edge is outside
        (5000, 10, Outcome.OUTSIDE_RADIUS, ()),
        (10, 100, Outcome.VERIFIED, ()),  # accuracy exactly at the accepted limit
        (10, 100.5, Outcome.INCONCLUSIVE, (Reason.LOW_ACCURACY,)),  # worse than accepted, even inside
        (9000, 2000, Outcome.INCONCLUSIVE, (Reason.LOW_ACCURACY,)),  # a desktop's IP-based fix
    ],
)
def test_one_office_boundaries(
    metres: float, accuracy: float, outcome: Outcome, reasons: tuple[Reason, ...]
) -> None:
    place = office()
    verdict = judge_office(place, north_of(place, metres, accuracy=accuracy))
    assert (verdict.outcome, verdict.reasons) == (outcome, reasons)
    assert verdict.review_required is (outcome is not Outcome.VERIFIED)


def test_stored_values_are_reduced() -> None:
    assert stored_distance(0) == (0, False)
    assert stored_distance(154.9) == (150, False)
    assert stored_distance(155.0) == (160, False)
    assert stored_distance(DISTANCE_CAP_M) == (DISTANCE_CAP_M, False)
    assert stored_distance(DISTANCE_CAP_M + 1) == (DISTANCE_CAP_M, True)
    assert stored_distance(4_000_000) == (DISTANCE_CAP_M, True)
    # Accuracy is never shown better than reported.
    assert stored_accuracy(0.4) == 5
    assert stored_accuracy(12) == 15
    assert stored_accuracy(15) == 15
    assert stored_accuracy(50_000) == 10_000
    place = office()
    far = judge_office(place, north_of(place, 40_000))
    assert (far.distance_m, far.distance_over_cap) == (DISTANCE_CAP_M, True)


def test_position_age_uses_the_devices_own_clock() -> None:
    place = office()
    assert judge_office(place, north_of(place, 10, age=120)).outcome is Outcome.VERIFIED
    stale = judge_office(place, north_of(place, 10, age=121))
    assert (stale.outcome, stale.reasons) == (Outcome.UNVERIFIED, (Reason.STALE_POSITION,))
    assert (stale.distance_m, stale.accuracy_m, stale.position_age_seconds) == (None, None, 121)
    # A device clock that is hours wrong does not matter: both times are on it.
    shifted = north_of(place, 10, age=5)
    skewed = Position(
        shifted.latitude,
        shifted.longitude,
        shifted.accuracy_m,
        shifted.captured_at - timedelta(hours=7),
        shifted.sent_at - timedelta(hours=7),
    )
    assert judge_office(place, skewed).outcome is Outcome.VERIFIED
    # A capture after sending is a clock fault, not a fresh position.
    backwards = north_of(place, 10, age=-6)
    assert judge_office(place, backwards).reasons == (Reason.INVALID_TIMESTAMP,)
    assert judge_office(place, north_of(place, 10, age=-4)).outcome is Outcome.VERIFIED


@pytest.mark.parametrize(
    "failure",
    [Reason.PERMISSION_DENIED, Reason.UNAVAILABLE, Reason.TIMEOUT, Reason.UNSUPPORTED, Reason.SKIPPED],
)
def test_browser_failures_are_unverified_and_reviewed_when_the_check_says_so(failure: Reason) -> None:
    flagged = judge([office()], failure)
    assert flagged is not None
    assert (flagged.outcome, flagged.reasons, flagged.review_required) == (
        Outcome.UNVERIFIED,
        (failure,),
        True,
    )
    quiet = judge([office(review_unverified=False)], failure)
    assert quiet is not None
    assert quiet.review_required is False


def test_no_report_from_an_older_client_is_not_provided() -> None:
    verdict = judge([office()], None)
    assert verdict is not None
    assert verdict.reasons == (Reason.NOT_PROVIDED,)
    # A reason that is not a browser failure is never taken from the client as one.
    assert judge([office()], Reason.BOUNDARY).reasons == (Reason.NOT_PROVIDED,)  # type: ignore[union-attr]


def test_no_office_with_a_check_means_no_verdict() -> None:
    assert judge([], None) is None
    assert judge([], Reason.SKIPPED) is None
    assert judge([], north_of(office(), 10)) is None


def test_the_best_office_wins_deterministically() -> None:
    home = office(code="TEST-HOME", placed=True)
    # Another office 1 km north of home.
    other = office(code="TEST-OTHER", latitude=home.latitude + 1000 / M_PER_DEGREE)
    at_other = north_of(other, 20)
    verdict = judge([home, other], at_other)
    assert verdict is not None
    assert (verdict.office.code, verdict.outcome) == ("TEST-OTHER", Outcome.VERIFIED)
    # Outside both: the nearer office relative to its radius is named.
    between = north_of(home, 400)
    nearer = judge([other, home], between)
    assert nearer is not None
    assert (nearer.office.code, nearer.outcome) == ("TEST-HOME", Outcome.OUTSIDE_RADIUS)
    # Inside both overlapping offices: the one with the larger margin, whatever the order given.
    wide = office(code="TEST-WIDE", radius_m=1000)
    inside_both = north_of(home, 50)
    picks = {judge(order, inside_both).office.code for order in ([home, wide], [wide, home])}  # type: ignore[union-attr]
    assert picks == {"TEST-WIDE"}
    # Unverified against several offices names the placed one, then the lowest code.
    named = judge([other, home], Reason.TIMEOUT)
    assert named is not None
    assert named.office.code == "TEST-HOME"
    unplaced = judge([office(code="TEST-B"), office(code="TEST-A")], Reason.TIMEOUT)
    assert unplaced is not None
    assert unplaced.office.code == "TEST-A"


def test_a_verified_office_beats_a_nearer_inconclusive_one() -> None:
    strict = office(code="TEST-STRICT", max_accuracy_m=20)
    lenient = office(code="TEST-LENIENT", max_accuracy_m=100)
    verdict = judge([strict, lenient], north_of(strict, 10, accuracy=50))
    assert verdict is not None
    assert (verdict.office.code, verdict.outcome) == ("TEST-LENIENT", Outcome.VERIFIED)
