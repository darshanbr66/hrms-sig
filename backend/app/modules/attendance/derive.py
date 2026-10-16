"""Judging an attendance day: `derive_day` is a pure function over the day's events, the shift
and attendance policy effective on that day, and the calendar (docs/database-design.md §4.5,
product-requirements ATT-4 to ATT-6, ATT-12).

Every threshold, grace period and rule comes from its parameters (CLAUDE.md rule 18); nothing
here is a Sigvitas value. The mechanism, fixed by the architecture:

- Without a shift or an attendance policy the day is not judged (`configured` is False): no
  default is applied (ATT-12). Durations from the events are still reported.
- Precedence: approved full-day leave, then a holiday, then a weekly off, then a pending
  correction. Work on a leave day, holiday or weekly off is kept as worked time with an
  anomaly (`leave_day_work`, `holiday_work`, `weekly_off_work`).
- A working day is judged once it has closed: the shift's end plus the policy's
  `incomplete_after_minutes`. Before then the status is None (not judged yet).
- A closed day with no events is `absent`; with a session that was never clocked out it is
  `incomplete`, and the system never invents a clock-out (ATT-6). Otherwise the policy's
  duration basis (effective or gross minutes) is compared with the full-day and half-day
  thresholds.
- Half-day leave: the worked time is judged against the half-day threshold; meeting it gives
  `present` (leave plus work make the day), otherwise `half_day` (the leave half). Late
  arrival and early departure are not measured on such a day, since the leave may cover
  either half.
- Gross minutes are the clocked-in sessions; break minutes the breaks inside them; effective
  minutes are gross minus breaks, or gross when breaks count as work. When breaks exceed the
  policy's maximum the day is flagged `break_exceeded`, and with `deduct` the excess is also
  taken from effective minutes when breaks otherwise count as work.
- Late minutes: `max(0, first in - (shift start + grace))`. Early departure:
  `max(0, (shift end - early grace) - last out)`.
- Overtime only when enabled: beyond the full-day threshold (on the policy's basis) or beyond
  the shift's end, counted only from the policy's minimum, flagged when it needs approval.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.modules.attendance import timeline

FULL_DAY: Final = Decimal("1")


class DayStatus(StrEnum):
    PRESENT = "present"
    HALF_DAY = "half_day"
    ABSENT = "absent"
    ON_LEAVE = "on_leave"
    HOLIDAY = "holiday"
    WEEKLY_OFF = "weekly_off"
    INCOMPLETE = "incomplete"
    PENDING_CORRECTION = "pending_correction"


@dataclass(frozen=True, slots=True)
class WeeklyOff:
    """A weekday off (Monday is 1), every week or only in some weeks of the month (the first
    such weekday of a month is week 1)."""

    weekday: int
    weeks: frozenset[int] | None = None

    def matches(self, day: date) -> bool:
        if day.isoweekday() != self.weekday:
            return False
        return self.weeks is None or (day.day - 1) // 7 + 1 in self.weeks


@dataclass(frozen=True, slots=True)
class ShiftTimes:
    start: time
    end: time
    crosses_midnight: bool
    weekly_offs: tuple[WeeklyOff, ...] = ()

    def is_weekly_off(self, day: date) -> bool:
        return any(off.matches(day) for off in self.weekly_offs)

    def window(self, work_date: date, zone: tzinfo) -> tuple[datetime, datetime]:
        """The shift's start and end on a work date, as aware times. A shift crossing midnight
        ends on the next calendar day; the work date is the day it starts (ATT-9)."""
        start = datetime.combine(work_date, self.start, zone)
        end_date = work_date + timedelta(days=1) if self.crosses_midnight else work_date
        return start, datetime.combine(end_date, self.end, zone)


@dataclass(frozen=True, slots=True)
class PolicyValues:
    grace_minutes: int
    early_departure_grace_minutes: int
    duration_basis: str  # effective | gross
    full_day_min_minutes: int
    half_day_min_minutes: int
    max_break_minutes: int | None
    breaks_count_as_work: bool
    break_excess_action: str  # flag | deduct
    overtime_enabled: bool
    overtime_basis: str | None  # beyond_full_day | beyond_shift_end
    overtime_min_minutes: int | None
    overtime_requires_approval: bool
    incomplete_after_minutes: int
    allow_multiple_sessions: bool


@dataclass(frozen=True, slots=True)
class DayContext:
    work_date: date
    zone: tzinfo
    shift: ShiftTimes | None
    policy: PolicyValues | None
    holiday: bool = False
    # Declared in the location's holiday calendar as a working day: the shift's weekly off
    # does not apply on this date (revision 0027).
    declared_working_day: bool = False
    # Approved leave on the day: 0, 0.5 or 1.
    leave_portion: Decimal = Decimal("0")
    pending_correction: bool = False
    # Joined on or before the date and no last working day before it.
    employed: bool = True


@dataclass(frozen=True, slots=True)
class DaySummary:
    status: DayStatus | None
    configured: bool
    first_in: datetime | None
    last_out: datetime | None
    shift_start_at: datetime | None
    shift_end_at: datetime | None
    gross_minutes: int
    break_minutes: int
    effective_minutes: int
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    open_session: bool
    anomalies: tuple[str, ...]


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds()) // 60)


@dataclass(frozen=True, slots=True)
class _Durations:
    totals: timeline.Totals
    gross: int
    breaks: int
    effective: int
    anomalies: tuple[str, ...]


def _durations(
    ordered: Sequence[timeline.Event], policy: PolicyValues | None, measure_until: datetime
) -> _Durations:
    totals = timeline.totals(ordered, measure_until)
    gross = totals.session_seconds // 60
    breaks = totals.break_seconds // 60
    anomalies = ["missing_break_end"] if totals.missing_break_end else []
    effective = gross - breaks
    if policy is not None:
        if policy.breaks_count_as_work:
            effective = gross
        if policy.max_break_minutes is not None and breaks > policy.max_break_minutes:
            anomalies.append("break_exceeded")
            if policy.break_excess_action == "deduct" and policy.breaks_count_as_work:
                effective = gross - (breaks - policy.max_break_minutes)
    return _Durations(totals, gross, breaks, max(0, effective), tuple(anomalies))


def _calendar_status(context: DayContext, shift: ShiftTimes) -> tuple[DayStatus, str] | None:
    """Leave, a holiday or a weekly off, with the anomaly worked time on it raises."""
    if context.leave_portion >= FULL_DAY:
        return DayStatus.ON_LEAVE, "leave_day_work"
    if context.holiday:
        return DayStatus.HOLIDAY, "holiday_work"
    if shift.is_weekly_off(context.work_date) and not context.declared_working_day:
        return DayStatus.WEEKLY_OFF, "weekly_off_work"
    return None


def _worked_status(basis: int, policy: PolicyValues, half_leave: bool) -> DayStatus:
    if half_leave:
        return DayStatus.PRESENT if basis >= policy.half_day_min_minutes else DayStatus.HALF_DAY
    if basis >= policy.full_day_min_minutes:
        return DayStatus.PRESENT
    if basis >= policy.half_day_min_minutes:
        return DayStatus.HALF_DAY
    return DayStatus.ABSENT


def _overtime(basis: int, last_out: datetime | None, shift_end: datetime, policy: PolicyValues) -> int:
    if not policy.overtime_enabled or policy.overtime_min_minutes is None:
        return 0
    if policy.overtime_basis == "beyond_full_day":
        candidate = basis - policy.full_day_min_minutes
    else:
        candidate = _minutes(last_out - shift_end) if last_out is not None else 0
    return candidate if candidate > 0 and candidate >= policy.overtime_min_minutes else 0


def derive_day(events: Sequence[timeline.Event], context: DayContext, now: datetime) -> DaySummary:
    shift, policy = context.shift, context.policy
    window = shift.window(context.work_date, context.zone) if shift else None
    closes_at = window[1] + timedelta(minutes=policy.incomplete_after_minutes) if window and policy else None
    closed = closes_at is not None and now >= closes_at
    ordered = sorted(events, key=lambda event: event.occurred_at)
    # A closed day's open session counts nothing past its last event: the clock-out is unknown.
    durations = _durations(ordered, policy, ordered[-1].occurred_at if closed and ordered else now)
    totals = durations.totals
    anomalies = list(durations.anomalies)

    def summary(status: DayStatus | None, late: int = 0, early: int = 0, overtime: int = 0) -> DaySummary:
        return DaySummary(
            status=status,
            configured=shift is not None and policy is not None,
            first_in=totals.first_in,
            last_out=totals.last_out,
            shift_start_at=window[0] if window else None,
            shift_end_at=window[1] if window else None,
            gross_minutes=durations.gross,
            break_minutes=durations.breaks,
            effective_minutes=durations.effective,
            late_minutes=late,
            early_leave_minutes=early,
            overtime_minutes=overtime,
            open_session=totals.open_session,
            anomalies=tuple(anomalies),
        )

    if not context.employed or shift is None or policy is None or window is None:
        return summary(None)
    calendar = _calendar_status(context, shift)
    if calendar is not None:
        if ordered:
            anomalies.append(calendar[1])
        return summary(calendar[0])
    if context.pending_correction:
        return summary(DayStatus.PENDING_CORRECTION)
    if not closed or not ordered or totals.open_session:
        # Not judged until the day closes; then no events is absent, and a session never
        # clocked out is incomplete.
        if not closed:
            return summary(None)
        return summary(DayStatus.INCOMPLETE if ordered else DayStatus.ABSENT)

    basis = durations.effective if policy.duration_basis == "effective" else durations.gross
    half_leave = context.leave_portion > 0
    late = early = 0
    if half_leave:
        anomalies.append("half_day_leave")
    else:
        if totals.first_in is not None:
            late = _minutes(totals.first_in - (window[0] + timedelta(minutes=policy.grace_minutes)))
        if totals.last_out is not None:
            early = _minutes(
                (window[1] - timedelta(minutes=policy.early_departure_grace_minutes)) - totals.last_out
            )
    overtime = _overtime(basis, totals.last_out, window[1], policy)
    if overtime and policy.overtime_requires_approval:
        anomalies.append("overtime_needs_approval")
    return summary(_worked_status(basis, policy, half_leave), late, early, overtime)
