"""The attendance event sequence and the time between events (no policy involved)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.attendance import timeline
from app.modules.attendance.timeline import Event, EventType, SequenceError, State

START = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)


def at(minutes: int, event_type: EventType) -> Event:
    return Event(event_type, START + timedelta(minutes=minutes))


@pytest.mark.parametrize(
    ("state", "event_type", "after"),
    [
        (State.CLOCKED_OUT, EventType.CLOCK_IN, State.CLOCKED_IN),
        (State.CLOCKED_IN, EventType.BREAK_START, State.ON_BREAK),
        (State.ON_BREAK, EventType.BREAK_END, State.CLOCKED_IN),
        (State.CLOCKED_IN, EventType.CLOCK_OUT, State.CLOCKED_OUT),
        (State.ON_BREAK, EventType.CLOCK_OUT, State.CLOCKED_OUT),
    ],
)
def test_valid_sequences(state: State, event_type: EventType, after: State) -> None:
    assert timeline.next_state(state, event_type) is after


@pytest.mark.parametrize(
    ("state", "event_type"),
    [
        (State.CLOCKED_OUT, EventType.BREAK_START),
        (State.CLOCKED_OUT, EventType.BREAK_END),
        (State.CLOCKED_OUT, EventType.CLOCK_OUT),
        (State.CLOCKED_IN, EventType.CLOCK_IN),
        (State.CLOCKED_IN, EventType.BREAK_END),
        (State.ON_BREAK, EventType.CLOCK_IN),
        (State.ON_BREAK, EventType.BREAK_START),
    ],
)
def test_invalid_sequences_are_refused(state: State, event_type: EventType) -> None:
    with pytest.raises(SequenceError):
        timeline.next_state(state, event_type)


def test_state_after_the_last_event() -> None:
    assert timeline.state_after(None) is State.CLOCKED_OUT
    assert timeline.state_after(EventType.BREAK_START) is State.ON_BREAK


def test_a_closed_day_with_a_break() -> None:
    totals = timeline.totals(
        [
            at(0, EventType.CLOCK_IN),
            at(240, EventType.BREAK_START),
            at(270, EventType.BREAK_END),
            at(540, EventType.CLOCK_OUT),
        ],
        now=START + timedelta(hours=20),
    )
    assert totals.session_seconds == 540 * 60
    assert totals.break_seconds == 30 * 60
    assert totals.first_in == START
    assert totals.last_out == START + timedelta(minutes=540)
    assert not totals.open_session
    assert not totals.missing_break_end


def test_open_session_and_break_count_up_to_now_and_are_never_closed() -> None:
    totals = timeline.totals(
        [at(0, EventType.CLOCK_IN), at(60, EventType.BREAK_START)], now=START + timedelta(minutes=75)
    )
    assert totals.session_seconds == 75 * 60
    assert totals.break_seconds == 15 * 60
    assert totals.open_session
    assert totals.on_break
    assert totals.last_out is None


def test_several_sessions_add_up_and_gaps_count_as_neither() -> None:
    totals = timeline.totals(
        [
            at(0, EventType.CLOCK_IN),
            at(120, EventType.CLOCK_OUT),
            at(180, EventType.CLOCK_IN),
            at(240, EventType.CLOCK_OUT),
        ],
        now=START + timedelta(hours=10),
    )
    assert totals.session_seconds == 180 * 60
    assert totals.break_seconds == 0
    assert totals.first_in == START


def test_clocking_out_during_a_break_is_flagged() -> None:
    totals = timeline.totals(
        [at(0, EventType.CLOCK_IN), at(60, EventType.BREAK_START), at(90, EventType.CLOCK_OUT)],
        now=START + timedelta(hours=10),
    )
    assert totals.missing_break_end
    assert totals.break_seconds == 30 * 60
    assert totals.session_seconds == 90 * 60


def test_out_of_sequence_events_are_skipped_not_double_counted() -> None:
    totals = timeline.totals(
        [at(0, EventType.CLOCK_IN), at(10, EventType.CLOCK_IN), at(60, EventType.CLOCK_OUT)],
        now=START + timedelta(hours=10),
    )
    assert totals.session_seconds == 60 * 60


def test_events_are_ordered_by_time() -> None:
    totals = timeline.totals([at(60, EventType.CLOCK_OUT), at(0, EventType.CLOCK_IN)], now=START)
    assert totals.session_seconds == 60 * 60


def test_local_date_uses_the_location_zone() -> None:
    late_evening_utc = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
    assert timeline.local_date(late_evening_utc, ZoneInfo("Asia/Kolkata")) == date(2026, 10, 6)
    assert timeline.local_date(late_evening_utc, ZoneInfo("America/New_York")) == date(2026, 10, 5)
