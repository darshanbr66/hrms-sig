"""Leave rules as pure functions over policy values (CLAUDE.md rule 18).

Nothing here holds a Sigvitas value: the leave year start, the allocation, whether a balance
may go negative and by how much, half days, and whether holidays and weekly offs count all
come from the leave type and the effective policy passed in.

- `leave_year_start`: the first day of the leave year a date falls in.
- `count_days`: how each day of a request counts. A day is half when the request starts in
  the afternoon (`start_half = second`) or ends at midday (`end_half = first`); a one-day
  request names the half it takes. Holidays and weekly offs inside the range count only when
  the leave type says so.
- `allows_balance`: whether taking some days keeps the balance within the policy.
- `most_specific`: the policy that applies to an employee, from the candidates that match
  their location and employment type: a location match outranks an employment-type match,
  and a policy naming both outranks either.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Protocol

FULL: Final = Decimal("1.00")
HALF: Final = Decimal("0.50")
# An input bound, not a policy: a request longer than this is a mistake in the dates.
MAX_REQUEST_DAYS: Final = 366


class RuleError(Exception):
    """A request that breaks a rule: the field, a stable code and a message for the person."""

    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CountedDay:
    date: date
    portion: Decimal
    counted: bool
    # holiday | weekly_off, when not counted.
    not_counted_reason: str | None


class PolicyScope(Protocol):
    @property
    def location_id(self) -> object | None: ...

    @property
    def employment_type(self) -> str | None: ...


def valid_year_start(month: int, day: int) -> bool:
    """A month and day that exist in every year (29 February does not)."""
    try:
        date(2001, month, day)
    except ValueError:
        return False
    return True


def leave_year_start(on: date, month: int, day: int) -> date:
    start = date(on.year, month, day)
    return start if start <= on else date(on.year - 1, month, day)


def _portions(start: date, end: date, start_half: str | None, end_half: str | None) -> dict[date, Decimal]:
    if start == end:
        if start_half and end_half and start_half != end_half:
            raise RuleError("end_half", "leave.half_mismatch", "Choose one half of the day.")
        return {start: HALF if (start_half or end_half) else FULL}
    if start_half == "first":
        raise RuleError(
            "start_half",
            "leave.start_half",
            "A request over several days can start in the afternoon (second half) or with a full day.",
        )
    if end_half == "second":
        raise RuleError(
            "end_half",
            "leave.end_half",
            "A request over several days can end at midday (first half) or with a full day.",
        )
    portions = {}
    day = start
    while day <= end:
        portions[day] = FULL
        day += timedelta(days=1)
    if start_half == "second":
        portions[start] = HALF
    if end_half == "first":
        portions[end] = HALF
    return portions


def count_days(
    start: date,
    end: date,
    start_half: str | None,
    end_half: str | None,
    *,
    allows_half_day: bool,
    holidays: Collection[date],
    counts_holidays: bool,
    weekly_offs: Collection[date],
    counts_weekly_offs: bool,
) -> list[CountedDay]:
    if end < start:
        raise RuleError("end_date", "leave.range_reversed", "The last day must be on or after the first day.")
    if (end - start).days + 1 > MAX_REQUEST_DAYS:
        raise RuleError(
            "end_date", "leave.range_too_long", f"Choose at most {MAX_REQUEST_DAYS} days in one request."
        )
    if (start_half or end_half) and not allows_half_day:
        raise RuleError("start_half", "leave.half_day_not_allowed", "This leave type is taken in whole days.")
    portions = _portions(start, end, start_half, end_half)
    days = []
    for day, portion in portions.items():
        if day in holidays and not counts_holidays:
            days.append(CountedDay(day, portion, False, "holiday"))
        elif day in weekly_offs and not counts_weekly_offs:
            days.append(CountedDay(day, portion, False, "weekly_off"))
        else:
            days.append(CountedDay(day, portion, True, None))
    return days


def total(days: Sequence[CountedDay]) -> Decimal:
    return sum((day.portion for day in days if day.counted), Decimal("0.00"))


def allows_balance(
    available: Decimal, days: Decimal, *, allow_negative: bool, max_negative: Decimal | None
) -> bool:
    """Whether `available - days` stays at or above zero, or above the allowed negative limit."""
    after = available - days
    if after >= 0:
        return True
    return allow_negative and max_negative is not None and after >= -max_negative


def most_specific[P: PolicyScope](
    candidates: Sequence[P], location_id: object | None, employment_type: str | None
) -> P | None:
    matching = [
        p
        for p in candidates
        if (p.location_id is None or p.location_id == location_id)
        and (p.employment_type is None or p.employment_type == employment_type)
    ]
    if not matching:
        return None
    return max(matching, key=lambda p: (p.location_id is not None) * 2 + (p.employment_type is not None))
