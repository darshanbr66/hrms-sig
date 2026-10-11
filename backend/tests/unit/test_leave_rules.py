"""Leave rules over policy values (app/modules/leave/rules.py). The values here are test values,
not Sigvitas policy: the point is that every rule follows its inputs."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.modules.leave.rules import (
    MAX_REQUEST_DAYS,
    RuleError,
    allows_balance,
    count_days,
    leave_year_start,
    most_specific,
    total,
    valid_year_start,
)


def counted(start: date, end: date, start_half: str | None = None, end_half: str | None = None, **kw: object):  # type: ignore[no-untyped-def]
    options: dict[str, object] = {
        "allows_half_day": True,
        "holidays": set(),
        "counts_holidays": False,
        "weekly_offs": set(),
        "counts_weekly_offs": False,
    }
    options.update(kw)
    return count_days(start, end, start_half, end_half, **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("on", "month", "day", "expected"),
    [
        (date(2026, 10, 11), 1, 1, date(2026, 1, 1)),
        (date(2026, 3, 31), 4, 1, date(2025, 4, 1)),
        (date(2026, 4, 1), 4, 1, date(2026, 4, 1)),
        (date(2026, 12, 31), 12, 31, date(2026, 12, 31)),
    ],
)
def test_the_leave_year_follows_the_policy_start(on: date, month: int, day: int, expected: date) -> None:
    assert leave_year_start(on, month, day) == expected


def test_a_leave_year_cannot_start_on_a_day_missing_from_some_years() -> None:
    assert valid_year_start(4, 1)
    assert not valid_year_start(2, 29)
    assert not valid_year_start(4, 31)


def test_whole_and_half_days() -> None:
    assert total(counted(date(2026, 11, 2), date(2026, 11, 4))) == Decimal(3)
    assert total(counted(date(2026, 11, 2), date(2026, 11, 2), "first")) == Decimal("0.5")
    assert total(counted(date(2026, 11, 2), date(2026, 11, 4), "second", "first")) == Decimal(2)
    with pytest.raises(RuleError) as error:
        counted(date(2026, 11, 2), date(2026, 11, 4), "first")
    assert error.value.code == "leave.start_half"
    with pytest.raises(RuleError) as error:
        counted(date(2026, 11, 2), date(2026, 11, 4), None, "second")
    assert error.value.code == "leave.end_half"
    with pytest.raises(RuleError) as error:
        counted(date(2026, 11, 2), date(2026, 11, 2), "first", "second")
    assert error.value.code == "leave.half_mismatch"
    with pytest.raises(RuleError) as error:
        counted(date(2026, 11, 2), date(2026, 11, 2), "first", allows_half_day=False)
    assert error.value.code == "leave.half_day_not_allowed"


def test_holidays_and_weekly_offs_count_only_when_the_type_says_so() -> None:
    holiday, off = date(2026, 11, 3), date(2026, 11, 4)
    days = counted(date(2026, 11, 2), date(2026, 11, 5), holidays={holiday}, weekly_offs={off})
    assert [(d.date, d.counted, d.not_counted_reason) for d in days] == [
        (date(2026, 11, 2), True, None),
        (holiday, False, "holiday"),
        (off, False, "weekly_off"),
        (date(2026, 11, 5), True, None),
    ]
    assert total(days) == Decimal(2)
    both = counted(
        date(2026, 11, 2),
        date(2026, 11, 5),
        holidays={holiday},
        weekly_offs={off},
        counts_holidays=True,
        counts_weekly_offs=True,
    )
    assert total(both) == Decimal(4)


def test_ranges_are_checked() -> None:
    with pytest.raises(RuleError) as error:
        counted(date(2026, 11, 5), date(2026, 11, 2))
    assert error.value.code == "leave.range_reversed"
    with pytest.raises(RuleError) as error:
        counted(date(2026, 1, 1), date(2027, 1, 2))
    assert error.value.code == "leave.range_too_long"
    assert MAX_REQUEST_DAYS == 366


@pytest.mark.parametrize(
    ("available", "days", "allow", "limit", "expected"),
    [
        ("5", "5", False, None, True),
        ("5", "5.5", False, None, False),
        ("1", "3", True, "2", True),
        ("1", "3.5", True, "2", False),
        ("0", "0.5", True, "0", False),
        ("-1", "1", True, "2", True),
    ],
)
def test_the_balance_limit_follows_the_policy(
    available: str, days: str, allow: bool, limit: str | None, expected: bool
) -> None:
    assert (
        allows_balance(
            Decimal(available),
            Decimal(days),
            allow_negative=allow,
            max_negative=Decimal(limit) if limit is not None else None,
        )
        is expected
    )


@dataclass(frozen=True)
class Scope:
    name: str
    location_id: uuid.UUID | None
    employment_type: str | None


def test_the_most_specific_policy_wins() -> None:
    here, there = uuid.uuid4(), uuid.uuid4()
    general = Scope("general", None, None)
    by_type = Scope("type", None, "contract")
    by_location = Scope("location", here, None)
    both = Scope("both", here, "contract")
    elsewhere = Scope("elsewhere", there, None)
    policies = [general, by_type, by_location, both, elsewhere]
    assert most_specific(policies, here, "contract") is both
    assert most_specific(policies, here, "full_time") is by_location
    assert most_specific(policies, uuid.uuid4(), "contract") is by_type
    assert most_specific(policies, uuid.uuid4(), "intern") is general
    assert most_specific([elsewhere], here, None) is None
