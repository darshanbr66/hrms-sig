"""Accrual periods, proration, the year end and carried-day expiry as pure functions over test
policy values (product-requirements §6.12). Every amount is an obviously fake test value."""

from datetime import date
from decimal import Decimal

import pytest

from app.modules.leave.rules import (
    Period,
    accrual_periods,
    add_months,
    expired_carry,
    prorated,
    round_days,
    year_end,
)

D = Decimal


def test_accrual_periods_follow_the_leave_year_start() -> None:
    monthly = accrual_periods(date(2031, 4, 1), "monthly")
    assert len(monthly) == 12
    assert monthly[0] == Period(date(2031, 4, 1), date(2031, 4, 30))
    assert monthly[-1] == Period(date(2032, 3, 1), date(2032, 3, 31))
    quarterly = accrual_periods(date(2031, 1, 15), "quarterly")
    assert [p.start for p in quarterly] == [
        date(2031, 1, 15),
        date(2031, 4, 15),
        date(2031, 7, 15),
        date(2031, 10, 15),
    ]
    assert quarterly[-1].end == date(2032, 1, 14)


def test_add_months_keeps_the_day_or_the_month_end() -> None:
    assert add_months(date(2031, 1, 31), 1) == date(2031, 2, 28)
    assert add_months(date(2031, 11, 30), 3) == date(2032, 2, 29)


@pytest.mark.parametrize(
    ("amount", "rule", "expected"),
    [
        (D("1.37"), "exact", D("1.37")),
        (D("1.37"), "nearest_half", D("1.50")),
        (D("1.20"), "nearest_half", D("1.00")),
        (D("1.99"), "down_half", D("1.50")),
        (D("1.01"), "up_half", D("1.50")),
    ],
)
def test_rounding_rules(amount: Decimal, rule: str, expected: Decimal) -> None:
    assert round_days(amount, rule) == expected


def test_proration_only_when_the_policy_says_so() -> None:
    april = Period(date(2031, 4, 1), date(2031, 4, 30))
    joined_mid_month = date(2031, 4, 16)
    assert prorated(
        D("3.00"), april, joined_mid_month, None, on_joining=False, on_exit=False, rounding=None
    ) == D("3.00")
    assert prorated(
        D("3.00"), april, joined_mid_month, None, on_joining=True, on_exit=False, rounding="exact"
    ) == D("1.50")
    left_mid_month = date(2031, 4, 10)
    share = prorated(
        D("3.00"),
        april,
        date(2020, 1, 1),
        left_mid_month,
        on_joining=True,
        on_exit=True,
        rounding="down_half",
    )
    assert share == D("1.00")
    assert (
        prorated(D("3.00"), april, date(2031, 5, 1), None, on_joining=True, on_exit=True, rounding="exact")
        == 0
    )


def test_the_year_end_carries_up_to_the_cap_and_lapses_or_keeps_the_rest() -> None:
    capped = year_end(D("12.50"), mode="capped", cap=D("5"), excess_action="lapse")
    assert (capped.carried, capped.lapsed, capped.kept) == (D("5"), D("7.50"), D("0"))
    kept = year_end(D("12.50"), mode="capped", cap=D("5"), excess_action="keep")
    assert (kept.carried, kept.lapsed, kept.kept) == (D("5"), D("0"), D("7.50"))
    everything = year_end(D("12.50"), mode="unlimited", cap=None, excess_action=None)
    assert (everything.carried, everything.lapsed) == (D("12.50"), D("0"))
    nothing = year_end(D("3"), mode="none", cap=None, excess_action="lapse")
    assert (nothing.carried, nothing.lapsed) == (D("0"), D("3"))
    negative = year_end(D("-2"), mode="capped", cap=D("5"), excess_action="lapse")
    assert (negative.carried, negative.lapsed) == (D("0"), D("0"))


def test_carried_days_used_first_and_never_more_than_the_balance_expire() -> None:
    assert expired_carry(D("5"), used=D("2"), balance=D("10")) == D("3")
    assert expired_carry(D("5"), used=D("6"), balance=D("10")) == D("0")
    assert expired_carry(D("5"), used=D("0"), balance=D("1.5")) == D("1.5")
