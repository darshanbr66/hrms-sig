"""`derive_day` over test policies only (docs/database-design.md §4.5). Every value here is an
obviously fake test value; two different policies judge the same day differently, which shows
no threshold is built into the code (roadmap M3 exit)."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.modules.attendance.derive import (
    DayContext,
    DayStatus,
    PolicyValues,
    ShiftTimes,
    WeeklyOff,
    derive_day,
)
from app.modules.attendance.timeline import Event, EventType

ZONE = ZoneInfo("Asia/Kolkata")
DAY = date(2031, 3, 4)  # a Tuesday
TEST_POLICY = PolicyValues(
    grace_minutes=7,
    early_departure_grace_minutes=3,
    duration_basis="effective",
    full_day_min_minutes=400,
    half_day_min_minutes=200,
    max_break_minutes=45,
    breaks_count_as_work=False,
    break_excess_action="flag",
    overtime_enabled=True,
    overtime_basis="beyond_shift_end",
    overtime_min_minutes=11,
    overtime_requires_approval=True,
    incomplete_after_minutes=90,
    allow_multiple_sessions=False,
)
DAY_SHIFT = ShiftTimes(time(9, 0), time(17, 30), False, (WeeklyOff(7),))
NIGHT_SHIFT = ShiftTimes(time(22, 0), time(6, 0), True)
LATER = datetime(2031, 3, 10, tzinfo=UTC)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ZONE)


def events(*pairs: tuple[EventType, datetime]) -> list[Event]:
    return [Event(kind, moment) for kind, moment in pairs]


def context(**values: object) -> DayContext:
    base = DayContext(work_date=DAY, zone=ZONE, shift=DAY_SHIFT, policy=TEST_POLICY)
    return replace(base, **values)  # type: ignore[arg-type]


def test_a_full_day_with_late_arrival_and_overtime() -> None:
    day = events(
        (EventType.CLOCK_IN, at(DAY, 9, 20)),
        (EventType.BREAK_START, at(DAY, 13)),
        (EventType.BREAK_END, at(DAY, 13, 30)),
        (EventType.CLOCK_OUT, at(DAY, 18, 0)),
    )
    summary = derive_day(day, context(), LATER)
    assert summary.status is DayStatus.PRESENT
    assert summary.gross_minutes == 520
    assert summary.break_minutes == 30
    assert summary.effective_minutes == 490
    assert summary.late_minutes == 13  # 20 minutes after the start, 7 of grace
    assert summary.early_leave_minutes == 0
    assert summary.overtime_minutes == 30
    assert "overtime_needs_approval" in summary.anomalies


def test_the_same_day_under_a_different_policy_is_judged_differently() -> None:
    day = events((EventType.CLOCK_IN, at(DAY, 9, 20)), (EventType.CLOCK_OUT, at(DAY, 14, 0)))
    strict = derive_day(day, context(), LATER)
    lenient = derive_day(
        day,
        context(
            policy=replace(
                TEST_POLICY,
                grace_minutes=30,
                full_day_min_minutes=250,
                overtime_enabled=False,
                overtime_basis=None,
                overtime_min_minutes=None,
                overtime_requires_approval=False,
            )
        ),
        LATER,
    )
    assert strict.status is DayStatus.HALF_DAY
    assert strict.late_minutes == 13
    assert lenient.status is DayStatus.PRESENT
    assert lenient.late_minutes == 0


def test_without_a_shift_or_policy_nothing_is_judged() -> None:
    day = events((EventType.CLOCK_IN, at(DAY, 9)), (EventType.CLOCK_OUT, at(DAY, 17)))
    for missing in ({"shift": None}, {"policy": None}):
        summary = derive_day(day, context(**missing), LATER)
        assert summary.status is None
        assert not summary.configured
        assert summary.gross_minutes == 480


def test_a_missing_clock_out_is_incomplete_once_the_day_closes_and_never_invented() -> None:
    day = events((EventType.CLOCK_IN, at(DAY, 9)))
    before_close = at(DAY, 18, 30)
    after_close = at(DAY, 19, 1)
    assert derive_day(day, context(), before_close).status is None
    summary = derive_day(day, context(), after_close)
    assert summary.status is DayStatus.INCOMPLETE
    assert summary.last_out is None
    # The open session counts nothing beyond its last event.
    assert summary.gross_minutes == 0


def test_a_shift_crossing_midnight_belongs_to_the_day_it_starts() -> None:
    day = events(
        (EventType.CLOCK_IN, at(DAY, 22, 5)), (EventType.CLOCK_OUT, at(DAY + timedelta(days=1), 6, 10))
    )
    summary = derive_day(day, context(shift=NIGHT_SHIFT), LATER)
    assert summary.shift_end_at == at(DAY + timedelta(days=1), 6)
    assert summary.gross_minutes == 485
    assert summary.status is DayStatus.PRESENT
    assert summary.late_minutes == 0
    # Overtime counts only from the policy's minimum.
    assert summary.overtime_minutes == 0


def test_no_events_is_absent_after_the_day_closes() -> None:
    assert derive_day([], context(), LATER).status is DayStatus.ABSENT
    assert derive_day([], context(), at(DAY, 12)).status is None


def test_precedence_leave_then_holiday_then_weekly_off() -> None:
    sunday = date(2031, 3, 9)
    worked = events((EventType.CLOCK_IN, at(sunday, 9)), (EventType.CLOCK_OUT, at(sunday, 12)))
    assert (
        derive_day([], context(leave_portion=Decimal("1"), holiday=True), LATER).status is DayStatus.ON_LEAVE
    )
    assert derive_day([], context(holiday=True), LATER).status is DayStatus.HOLIDAY
    weekly = derive_day(worked, context(work_date=sunday), LATER)
    assert weekly.status is DayStatus.WEEKLY_OFF
    assert "weekly_off_work" in weekly.anomalies
    assert weekly.gross_minutes == 180


def test_a_declared_working_day_is_judged_although_the_shift_has_it_off() -> None:
    sunday = date(2031, 3, 9)
    worked = events((EventType.CLOCK_IN, at(sunday, 9)), (EventType.CLOCK_OUT, at(sunday, 18)))
    assert derive_day([], context(work_date=sunday), LATER).status is DayStatus.WEEKLY_OFF
    declared = derive_day(worked, context(work_date=sunday, declared_working_day=True), LATER)
    assert declared.status is DayStatus.PRESENT
    assert "weekly_off_work" not in declared.anomalies
    assert (
        derive_day([], context(work_date=sunday, declared_working_day=True), LATER).status is DayStatus.ABSENT
    )
    # A holiday still wins: the declaration only cancels the weekly off.
    assert derive_day(
        [], context(work_date=sunday, declared_working_day=True, holiday=True), LATER
    ).status is (DayStatus.HOLIDAY)


def test_weekly_offs_in_some_weeks_of_the_month() -> None:
    second_and_fourth_saturdays = (WeeklyOff(6, frozenset({2, 4})),)
    shift = replace(DAY_SHIFT, weekly_offs=second_and_fourth_saturdays)
    saturdays = [date(2031, 3, day) for day in (1, 8, 15, 22, 29)]
    assert [shift.is_weekly_off(day) for day in saturdays] == [False, True, False, True, False]


def test_half_day_leave_is_judged_against_the_half_day_threshold() -> None:
    day = events((EventType.CLOCK_IN, at(DAY, 13, 30)), (EventType.CLOCK_OUT, at(DAY, 17, 30)))
    summary = derive_day(day, context(leave_portion=Decimal("0.5")), LATER)
    assert summary.status is DayStatus.PRESENT
    assert summary.late_minutes == 0
    assert "half_day_leave" in summary.anomalies
    short = events((EventType.CLOCK_IN, at(DAY, 16)), (EventType.CLOCK_OUT, at(DAY, 17, 30)))
    assert derive_day(short, context(leave_portion=Decimal("0.5")), LATER).status is DayStatus.HALF_DAY


def test_breaks_over_the_limit_are_flagged_or_deducted() -> None:
    day = events(
        (EventType.CLOCK_IN, at(DAY, 9)),
        (EventType.BREAK_START, at(DAY, 12)),
        (EventType.BREAK_END, at(DAY, 13, 15)),
        (EventType.CLOCK_OUT, at(DAY, 17, 30)),
    )
    flagged = derive_day(day, context(), LATER)
    assert "break_exceeded" in flagged.anomalies
    assert flagged.effective_minutes == 510 - 75
    deducting = replace(TEST_POLICY, breaks_count_as_work=True, break_excess_action="deduct")
    deducted = derive_day(day, context(policy=deducting), LATER)
    assert deducted.effective_minutes == 510 - 30


def test_a_pending_correction_and_unemployed_days() -> None:
    assert derive_day([], context(pending_correction=True), LATER).status is DayStatus.PENDING_CORRECTION
    assert derive_day([], context(employed=False), LATER).status is None
