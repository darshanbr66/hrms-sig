"""What an attendance correction may change (product-requirements ATT-3, ATT-7)."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.attendance.correction_rules import Add, CorrectionRuleError, Existing, resulting_day
from app.modules.attendance.timeline import EventType

ZONE = ZoneInfo("Asia/Kolkata")
DAY = date(2031, 3, 4)
NOW = datetime(2031, 3, 6, tzinfo=ZONE)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), ZONE)


def run(existing: list[Existing], adds: list[Add], voids: list[uuid.UUID], **kwargs: object) -> list[str]:
    values = {"work_date": DAY, "zone": ZONE, "earlier_last": None, "later_first": None, "now": NOW} | kwargs
    events = resulting_day(existing, adds, voids, **values)  # type: ignore[arg-type]
    return [event.event_type.value for event in events]


def refused(
    code: str, existing: list[Existing], adds: list[Add], voids: list[uuid.UUID], **kwargs: object
) -> None:
    with pytest.raises(CorrectionRuleError) as caught:
        run(existing, adds, voids, **kwargs)
    assert caught.value.code == code


CLOCK_IN = Existing(uuid.uuid4(), EventType.CLOCK_IN, at(9))


def test_a_forgotten_clock_out_is_added() -> None:
    assert run([CLOCK_IN], [Add(EventType.CLOCK_OUT, at(18))], []) == ["CLOCK_IN", "CLOCK_OUT"]


def test_a_wrong_event_is_voided_and_replaced() -> None:
    assert run(
        [CLOCK_IN], [Add(EventType.CLOCK_IN, at(8, 30)), Add(EventType.CLOCK_OUT, at(17))], [CLOCK_IN.id]
    ) == [
        "CLOCK_IN",
        "CLOCK_OUT",
    ]


def test_nothing_impossible_is_accepted() -> None:
    refused("correction.empty", [CLOCK_IN], [], [])
    refused("correction.void_twice", [CLOCK_IN], [], [CLOCK_IN.id, CLOCK_IN.id])
    refused("correction.unknown_event", [CLOCK_IN], [], [uuid.uuid4()])
    refused("correction.in_future", [CLOCK_IN], [Add(EventType.CLOCK_OUT, NOW + timedelta(minutes=1))], [])
    refused(
        "correction.outside_day",
        [CLOCK_IN],
        [Add(EventType.CLOCK_OUT, at(9, day=DAY - timedelta(days=1)))],
        [],
    )
    refused("correction.same_time", [CLOCK_IN], [Add(EventType.CLOCK_OUT, at(9))], [])
    refused("correction.sequence", [CLOCK_IN], [Add(EventType.CLOCK_IN, at(10))], [])
    refused("correction.sequence", [], [Add(EventType.BREAK_START, at(10))], [])


def test_days_never_overlap() -> None:
    refused(
        "correction.overlaps_earlier",
        [],
        [Add(EventType.CLOCK_IN, at(1)), Add(EventType.CLOCK_OUT, at(2))],
        [],
        earlier_last=at(3),
    )
    refused(
        "correction.overlaps_later", [CLOCK_IN], [Add(EventType.CLOCK_OUT, at(23))], [], later_first=at(22)
    )


def test_an_open_session_may_stay_open_only_as_the_latest_day() -> None:
    assert run([], [Add(EventType.CLOCK_IN, at(9))], []) == ["CLOCK_IN"]
    refused(
        "correction.unclosed",
        [],
        [Add(EventType.CLOCK_IN, at(9))],
        [],
        later_first=at(9, day=DAY + timedelta(days=1)),
    )


def test_a_session_crossing_midnight_may_end_the_next_day() -> None:
    night = Existing(uuid.uuid4(), EventType.CLOCK_IN, at(22))
    assert run([night], [Add(EventType.CLOCK_OUT, at(6, day=DAY + timedelta(days=1)))], []) == [
        "CLOCK_IN",
        "CLOCK_OUT",
    ]
