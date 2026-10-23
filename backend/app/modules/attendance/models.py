"""Attendance (docs/database-design.md §4.5): events; shifts, policies, corrections, voids and
locks; office location checks, office approvals, location evidence and its reviews (ADR-031).

Events, voids and correction items are append-only. Versioned documents (shift versions,
location default shifts, shift assignments, policies) change only to close an open range, and
are never deleted. A correction is decided or cancelled once; a lock is released once. These
update rules are enforced by the unit of work (formerly guard triggers); allowed values and
the rules within one document are part of each collection's validator. Overlapping ranges
(versions of one shift, defaults of one location, assignments of one person, policies of one
scope, active locks) are refused by the services, which write them only while holding a lock
on the owning document (formerly exclusion constraints).
"""

import dataclasses
import uuid
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, ClassVar, Final

from app.platform.db import CASE_INSENSITIVE, Index, Record, allowed, asc, utcnow

EVENT_TYPES: Final = ("CLOCK_IN", "BREAK_START", "BREAK_END", "CLOCK_OUT")
_OPEN_RANGE: Final = {
    "$or": [{"$eq": ["$effective_to", None]}, {"$gt": ["$effective_to", "$effective_from"]}]
}
_VERSIONED: Final = frozenset({"effective_to"})


class EventSource(StrEnum):
    WEB = "web"
    MOBILE = "mobile"
    CORRECTION = "correction"
    IMPORT = "import"


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendanceEvent(Record):
    __collection__ = "attendance_events"
    __append_only__ = True
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "idempotency_key"),
            name="uq_attendance_events_idempotency_key",
            unique=True,
        ),
        Index(
            asc("organization_id", "employee_id", "work_date", "occurred_at"),
            name="ix_attendance_events_employee_day",
        ),
        Index(asc("organization_id", "work_date"), name="ix_attendance_events_work_date"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "event_type": allowed(EVENT_TYPES),
        "source": allowed(EventSource),
        "user_agent": {"maxLength": 512},
        "idempotency_key": {"minLength": 8, "maxLength": 100},
    }
    __expr_rules__ = ({"$eq": [{"$eq": ["$source", "correction"]}, {"$ne": ["$correction_id", None]}]},)

    employee_id: uuid.UUID
    event_type: str
    # Server time for live events (product-requirements ATT-2); the approved time for
    # correction events.
    occurred_at: datetime
    work_date: date
    source: str
    recorded_by: uuid.UUID
    recorded_at: datetime = dataclasses.field(default_factory=utcnow)
    # Metadata only; never used as the event time.
    client_reported_at: datetime | None = None
    ip: str | None = None
    user_agent: str | None = None
    idempotency_key: str
    # Set on events an approved correction added (source `correction`).
    correction_id: uuid.UUID | None = None


class ShiftStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclasses.dataclass(kw_only=True, eq=False)
class Shift(Record):
    __collection__ = "shifts"
    __deletable__ = False
    __indexes__ = (
        Index(asc("organization_id", "code"), name="uq_shifts_code", unique=True, collation=CASE_INSENSITIVE),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": {"minLength": 1, "maxLength": 32, "pattern": r"^\S+$"},
        "name": {"minLength": 1, "maxLength": 200},
        "description": {"minLength": 1, "maxLength": 1000},
        "status": allowed(ShiftStatus),
        "version": {"minimum": 1},
    }

    code: str
    name: str
    description: str | None = None
    status: str = ShiftStatus.ACTIVE.value
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class ShiftVersion(Record):
    __collection__ = "shift_versions"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(
            asc("organization_id", "shift_id", "version_no"), name="uq_shift_versions_version_no", unique=True
        ),
        Index(asc("organization_id", "shift_id", "effective_from"), name="ix_shift_versions_shift"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "version_no": {"minimum": 1},
        "weekly_offs": {"maxItems": 7},
    }
    __expr_rules__ = (
        _OPEN_RANGE,
        {"$ne": ["$start_time", "$end_time"]},
        {"$eq": ["$crosses_midnight", {"$lt": ["$end_time", "$start_time"]}]},
    )

    shift_id: uuid.UUID
    version_no: int
    effective_from: date
    # Exclusive; None while open-ended.
    effective_to: date | None = None
    start_time: time
    end_time: time
    crosses_midnight: bool
    # [{"weekday": 1-7 (Monday is 1), "weeks": None or [1-5]}]: see `derive.WeeklyOff`.
    weekly_offs: list[dict[str, Any]]
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class LocationDefaultShift(Record):
    __collection__ = "location_default_shifts"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(
            asc("organization_id", "location_id", "effective_from"),
            name="ix_location_default_shifts_location",
        ),
        Index(asc("organization_id", "shift_id"), name="ix_location_default_shifts_shift_id"),
    )
    __expr_rules__ = (_OPEN_RANGE,)

    location_id: uuid.UUID
    shift_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class ShiftAssignment(Record):
    __collection__ = "shift_assignments"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(asc("organization_id", "employee_id", "effective_from"), name="ix_shift_assignments_employee"),
        Index(asc("organization_id", "shift_id"), name="ix_shift_assignments_shift_id"),
    )
    __expr_rules__ = (_OPEN_RANGE,)

    employee_id: uuid.UUID
    shift_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


class PolicyScope(StrEnum):
    ORGANIZATION = "organization"
    LOCATION = "location"
    SHIFT = "shift"


def _between(field: str, low: int, high: int) -> dict[str, Any]:
    return {"$and": [{"$gte": [f"${field}", low]}, {"$lte": [f"${field}", high]}]}


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendancePolicy(Record):
    """How a day is judged (product-requirements §6.12). Every value is Sigvitas's."""

    __collection__ = "attendance_policies"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(
            asc("organization_id", "scope_type", "scope_id", "effective_from"),
            name="ix_attendance_policies_scope",
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "scope_type": allowed(PolicyScope),
        "duration_basis": allowed(("effective", "gross")),
        "break_excess_action": allowed(("flag", "deduct")),
        "overtime_basis": allowed(("beyond_full_day", "beyond_shift_end")),
    }
    __expr_rules__ = (
        {
            "$or": [
                {"$and": [{"$eq": ["$scope_type", "organization"]}, {"$eq": ["$scope_id", None]}]},
                {"$and": [{"$in": ["$scope_type", ["location", "shift"]]}, {"$ne": ["$scope_id", None]}]},
            ]
        },
        _OPEN_RANGE,
        _between("grace_minutes", 0, 1440),
        _between("early_departure_grace_minutes", 0, 1440),
        _between("full_day_min_minutes", 1, 1440),
        _between("half_day_min_minutes", 1, 1440),
        {"$lte": ["$half_day_min_minutes", "$full_day_min_minutes"]},
        _between("incomplete_after_minutes", 0, 1440),
        {"$or": [{"$eq": ["$max_break_minutes", None]}, _between("max_break_minutes", 0, 1440)]},
        {
            "$or": [
                {
                    "$and": [
                        "$overtime_enabled",
                        {"$ne": ["$overtime_basis", None]},
                        _between("overtime_min_minutes", 0, 1440),
                    ]
                },
                {
                    "$and": [
                        {"$not": ["$overtime_enabled"]},
                        {"$eq": ["$overtime_basis", None]},
                        {"$eq": ["$overtime_min_minutes", None]},
                        {"$not": ["$overtime_requires_approval"]},
                    ]
                },
            ]
        },
    )

    scope_type: str
    scope_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None
    grace_minutes: int
    early_departure_grace_minutes: int
    duration_basis: str
    full_day_min_minutes: int
    half_day_min_minutes: int
    max_break_minutes: int | None = None
    breaks_count_as_work: bool
    break_excess_action: str
    overtime_enabled: bool
    overtime_basis: str | None = None
    overtime_min_minutes: int | None = None
    overtime_requires_approval: bool
    incomplete_after_minutes: int
    allow_multiple_sessions: bool
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


class CorrectionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CorrectionKind(StrEnum):
    # The employee asks; a manager or HR decides.
    REQUEST = "request"
    # HR records the change on the employee's behalf; it applies at once (SOD-7).
    HR_RECORDED = "hr_recorded"


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendanceCorrection(Record):
    __collection__ = "attendance_corrections"
    __deletable__ = False
    __mutable__ = frozenset({"status", "decided_by", "decided_at", "decision_note", "version"})
    __set_once__ = frozenset({"decided_by", "decided_at", "decision_note"})
    __erasable__ = frozenset({"reason", "decision_note"})
    __transitions__: ClassVar[dict[str, dict[str, frozenset[str]]]] = {
        "status": {"pending": frozenset({"approved", "rejected", "cancelled"})}
    }
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "idempotency_key"),
            name="uq_attendance_corrections_idempotency_key",
            unique=True,
        ),
        Index(
            asc("organization_id", "employee_id", "work_date"),
            name="uq_attendance_corrections_pending",
            unique=True,
            partial={"status": "pending"},
        ),
        Index(
            asc("organization_id", "status", "work_date"), name="ix_attendance_corrections_status_work_date"
        ),
        Index(
            asc("organization_id", "employee_id", "work_date"), name="ix_attendance_corrections_employee_day"
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "kind": allowed(CorrectionKind),
        "status": allowed(CorrectionStatus),
        "reason": {"minLength": 1, "maxLength": 1000},
        "decision_note": {"minLength": 1, "maxLength": 1000},
        "idempotency_key": {"minLength": 8, "maxLength": 100},
        "version": {"minimum": 1},
    }
    __expr_rules__ = (
        {"$eq": [{"$in": ["$status", ["pending", "cancelled"]]}, {"$eq": ["$decided_by", None]}]},
        {"$eq": [{"$eq": ["$decided_by", None]}, {"$eq": ["$decided_at", None]}]},
        {"$or": [{"$eq": ["$kind", "request"]}, {"$eq": ["$status", "approved"]}]},
    )

    employee_id: uuid.UUID
    work_date: date
    kind: str
    # The `personal` tier: shown to the employee and to whoever may decide the correction.
    reason: str
    status: str
    requested_by: uuid.UUID
    requested_at: datetime = dataclasses.field(default_factory=utcnow)
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    # The work date was inside an active attendance lock when the correction was recorded.
    locked_period: bool = False
    idempotency_key: str
    version: int = 1


class CorrectionAction(StrEnum):
    ADD = "add"
    VOID = "void"


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendanceCorrectionItem(Record):
    __collection__ = "attendance_correction_items"
    __append_only__ = True
    __indexes__ = (
        Index(asc("organization_id", "correction_id"), name="ix_attendance_correction_items_correction_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "action": allowed(CorrectionAction),
        "event_type": allowed(EVENT_TYPES),
    }
    __expr_rules__ = (
        {
            "$or": [
                {
                    "$and": [
                        {"$eq": ["$action", "add"]},
                        {"$ne": ["$event_type", None]},
                        {"$ne": ["$proposed_at", None]},
                        {"$eq": ["$target_event_id", None]},
                    ]
                },
                {
                    "$and": [
                        {"$eq": ["$action", "void"]},
                        {"$eq": ["$event_type", None]},
                        {"$eq": ["$proposed_at", None]},
                        {"$ne": ["$target_event_id", None]},
                    ]
                },
            ]
        },
    )

    correction_id: uuid.UUID
    action: str
    event_type: str | None = None
    proposed_at: datetime | None = None
    target_event_id: uuid.UUID | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendanceEventVoid(Record):
    """An event an approved correction voided. The event itself stays."""

    __collection__ = "attendance_event_voids"
    __append_only__ = True
    __indexes__ = (
        Index(asc("organization_id", "event_id"), name="uq_attendance_event_voids_event_id", unique=True),
    )

    event_id: uuid.UUID
    correction_id: uuid.UUID
    voided_by: uuid.UUID
    voided_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class AttendanceLock(Record):
    __collection__ = "attendance_locks"
    __deletable__ = False
    __mutable__ = frozenset({"released_by", "released_at", "release_reason"})
    __set_once__ = frozenset({"released_by", "released_at", "release_reason"})
    __indexes__ = (
        Index(asc("organization_id", "period_start", "period_end"), name="ix_attendance_locks_period"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "reason": {"minLength": 1, "maxLength": 500},
        "release_reason": {"minLength": 1, "maxLength": 500},
    }
    __expr_rules__ = (
        {"$gte": ["$period_end", "$period_start"]},
        {
            "$or": [
                {
                    "$and": [
                        {"$eq": ["$released_at", None]},
                        {"$eq": ["$released_by", None]},
                        {"$eq": ["$release_reason", None]},
                    ]
                },
                {
                    "$and": [
                        {"$ne": ["$released_at", None]},
                        {"$ne": ["$released_by", None]},
                        {"$ne": ["$release_reason", None]},
                    ]
                },
            ]
        },
    )

    period_start: date
    # Inclusive.
    period_end: date
    reason: str
    locked_by: uuid.UUID
    locked_at: datetime = dataclasses.field(default_factory=utcnow)
    released_by: uuid.UUID | None = None
    released_at: datetime | None = None
    release_reason: str | None = None


# --- office location verification (ADR-031) ----------------------------------------------------


class CheckMode(StrEnum):
    # A version that turns the office's check off from its date (history is kept).
    OFF = "off"
    VERIFY = "verify"


class ReviewDecision(StrEnum):
    ACCEPTED = "accepted"
    NOT_ACCEPTED = "not_accepted"


# A fixed list, so the review history is consistent and holds as little free text as possible.
REVIEW_REASONS: Final[dict[str, frozenset[str]]] = {
    "on_site_confirmed": frozenset({ReviewDecision.ACCEPTED}),
    "device_or_signal_issue": frozenset({ReviewDecision.ACCEPTED}),
    "approved_offsite_work": frozenset({ReviewDecision.ACCEPTED}),
    "not_on_site_correction_needed": frozenset({ReviewDecision.NOT_ACCEPTED}),
    "not_on_site_no_action": frozenset({ReviewDecision.NOT_ACCEPTED}),
    "other": frozenset({ReviewDecision.ACCEPTED, ReviewDecision.NOT_ACCEPTED}),
}
LOCATION_OUTCOMES: Final = ("verified", "outside_radius", "inconclusive", "unverified")
LOCATION_REASONS: Final = (
    "low_accuracy",
    "boundary",
    "permission_denied",
    "unavailable",
    "timeout",
    "unsupported",
    "skipped",
    "not_provided",
    "stale_position",
    "invalid_timestamp",
)
_MEASURED: Final = ("verified", "outside_radius", "inconclusive")


@dataclasses.dataclass(kw_only=True, eq=False)
class LocationCheck(Record):
    """One version of an office's location check: where the office is and how a reported
    position is judged against it. Every value is Sigvitas's (ADR-026). The office's position is
    organization data, not personal data. A version that already started is never edited; a new
    one starts on a date and closes the open one (`mode = off` turns the check off)."""

    __collection__ = "attendance_location_checks"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(
            asc("organization_id", "location_id", "effective_from"),
            name="ix_attendance_location_checks_location",
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {"mode": allowed(CheckMode)}
    __expr_rules__ = (
        _OPEN_RANGE,
        {"$and": [{"$gte": ["$latitude", -90]}, {"$lte": ["$latitude", 90]}]},
        {"$and": [{"$gte": ["$longitude", -180]}, {"$lte": ["$longitude", 180]}]},
        _between("radius_m", 10, 5000),
        _between("max_accuracy_m", 1, 5000),
        _between("max_position_age_seconds", 10, 600),
    )

    location_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    mode: str
    latitude: float
    longitude: float
    radius_m: int
    max_accuracy_m: int
    max_position_age_seconds: int
    # Whether events whose position could not be checked (denied, unavailable, stale...) are
    # sent to HR review.
    review_unverified: bool
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class OfficeApproval(Record):
    """An office a person may clock at besides the location of their job. Effective-dated; it
    ends by closing its range, never by deletion. One open approval per person and office."""

    __collection__ = "attendance_office_approvals"
    __deletable__ = False
    __mutable__ = _VERSIONED
    __set_once__ = _VERSIONED
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "location_id", "effective_from"),
            name="ix_attendance_office_approvals_employee",
        ),
        Index(
            asc("organization_id", "location_id", "effective_from"),
            name="ix_attendance_office_approvals_location",
        ),
    )
    __expr_rules__ = (_OPEN_RANGE,)

    employee_id: uuid.UUID
    location_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class LocationEvidence(Record):
    """What a clock event's reported position was judged as (append-only, one per event). No
    coordinates: the distance to the chosen office rounded to 10 m (at most 10 km), the reported
    accuracy rounded up to 5 m, the position's age on the device, and the configuration used."""

    __collection__ = "attendance_location_evidence"
    __append_only__ = True
    __indexes__ = (
        Index(asc("organization_id", "event_id"), name="uq_attendance_location_evidence_event", unique=True),
        Index(
            asc("organization_id", "review_required", "received_at", "_id"),
            name="ix_attendance_location_evidence_queue",
        ),
        Index(
            asc("organization_id", "employee_id", "work_date"),
            name="ix_attendance_location_evidence_employee",
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "event_type": allowed(EVENT_TYPES),
        "outcome": allowed(LOCATION_OUTCOMES),
        "reason_codes": {"maxItems": 3, "items": {"enum": list(LOCATION_REASONS)}},
    }
    __expr_rules__ = (
        # Measured outcomes carry a distance, an accuracy and an age; unverified ones no distance
        # and no accuracy, and always a reason.
        {
            "$or": [
                {
                    "$and": [
                        {"$in": ["$outcome", list(_MEASURED)]},
                        {"$ne": ["$distance_m", None]},
                        {"$ne": ["$accuracy_m", None]},
                        {"$ne": ["$position_age_seconds", None]},
                    ]
                },
                {
                    "$and": [
                        {"$eq": ["$outcome", "unverified"]},
                        {"$eq": ["$distance_m", None]},
                        {"$eq": ["$accuracy_m", None]},
                        {"$gt": [{"$size": "$reason_codes"}, 0]},
                    ]
                },
            ]
        },
        {"$or": [{"$ne": ["$outcome", "verified"]}, {"$not": ["$review_required"]}]},
        {"$or": [{"$ne": ["$outcome", "verified"]}, {"$eq": [{"$size": "$reason_codes"}, 0]}]},
        {"$or": [{"$eq": ["$distance_m", None]}, _between("distance_m", 0, 10000)]},
        {"$or": [{"$eq": ["$accuracy_m", None]}, _between("accuracy_m", 0, 10000)]},
        {"$or": [{"$not": ["$distance_over_cap"]}, {"$eq": ["$distance_m", 10000]}]},
    )

    event_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    event_type: str
    # The office and the check version the position was judged against.
    location_id: uuid.UUID
    check_id: uuid.UUID
    radius_m: int
    max_accuracy_m: int
    # How many offices the person could clock at with a check that day.
    offices_considered: int
    outcome: str
    reason_codes: list[str] = dataclasses.field(default_factory=list)
    distance_m: int | None = None
    distance_over_cap: bool = False
    accuracy_m: int | None = None
    position_age_seconds: int | None = None
    review_required: bool
    # Server time; the device's times are never trusted.
    received_at: datetime


@dataclasses.dataclass(kw_only=True, eq=False)
class LocationReview(Record):
    """An HR decision on one piece of evidence (append-only). The newest is the current one;
    each names the one it replaces, and only one review may replace a given one (unique index),
    so two reviewers can never both decide on the same state."""

    __collection__ = "attendance_location_reviews"
    __append_only__ = True
    __indexes__ = (
        Index(
            asc("organization_id", "evidence_id", "supersedes"),
            name="uq_attendance_location_reviews_chain",
            unique=True,
        ),
        Index(asc("organization_id", "employee_id"), name="ix_attendance_location_reviews_employee"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "decision": allowed(ReviewDecision),
        "reason_code": allowed(tuple(REVIEW_REASONS)),
        "note": {"minLength": 1, "maxLength": 500},
    }

    evidence_id: uuid.UUID
    employee_id: uuid.UUID
    decision: str
    reason_code: str
    # Optional; the form asks for no unnecessary personal details.
    note: str | None = None
    supersedes: uuid.UUID | None = None
    reviewed_by: uuid.UUID
    reviewed_at: datetime = dataclasses.field(default_factory=utcnow)
