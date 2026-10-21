"""Leave reports and export rows (product-requirements RPT-1 to RPT-3). Never the reason or a
decision note: reports carry dates, types, day counts and statuses only.

- `taken` (the leave report): per employee and leave type, the days of approved leave and of
  pending requests that fall in a period, counted as each request counted them.
- `request_rows` and `balance_rows`: the leave request and balance exports, for the employees
  the requester could read when the export was requested.
"""

import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from app.modules.leave.models import LeaveLedgerEntry, LeaveRequest, LeaveType, RequestStatus
from app.modules.people import public as people
from app.platform.csvfile import Cell
from app.platform.db import Document, Session, decode, encode_filter

REQUESTS_HEADER: Final = (
    "employee_code",
    "name",
    "leave_type",
    "start_date",
    "start_half",
    "end_date",
    "end_half",
    "days",
    "status",
    "requested_at",
)
BALANCES_HEADER: Final = ("employee_code", "name", "leave_type", "balance", "pending", "available")
_TAKEN: Final = (RequestStatus.APPROVED.value, RequestStatus.CANCELLATION_REQUESTED.value)
ZERO: Final = Decimal("0.00")
EARLIEST: Final = date(1900, 1, 1)


@dataclass(frozen=True, slots=True)
class TakenRow:
    colleague: people.Colleague
    leave_type_id: uuid.UUID
    leave_type_name: str
    taken: Decimal
    pending: Decimal


async def sums_by_type(
    session: Session, model: type[Document], field: str, match: dict[str, Any]
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """The exact sum of a decimal field per employee and leave type (Decimal128 arithmetic)."""
    pipeline = [
        {"$match": encode_filter(model, match)},
        {"$group": {"_id": {"e": "$employee_id", "t": "$leave_type_id"}, "total": {"$sum": f"${field}"}}},
    ]
    return {
        (row["_id"]["e"], row["_id"]["t"]): decode(Decimal, row["total"])
        for row in await session.aggregate(model, pipeline)
    }


async def _types(session: Session) -> dict[uuid.UUID, str]:
    return {leave_type.id: leave_type.name for leave_type in await session.find(LeaveType, {})}


async def taken(
    session: Session, colleagues: list[people.Colleague], start: date, end: date
) -> list[TakenRow]:
    ids = [c.id for c in colleagues]
    if not ids:
        return []
    requests = await session.find(
        LeaveRequest,
        {
            "employee_id": {"$in": ids},
            "status": {"$in": [*_TAKEN, RequestStatus.PENDING.value]},
            "start_date": {"$lte": end},
            "end_date": {"$gte": start},
        },
    )
    sums: dict[tuple[uuid.UUID, uuid.UUID], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for request in requests:
        days = sum(
            (day.portion for day in request.day_rows() if day.counted and start <= day.date <= end), ZERO
        )
        if days:
            sums[(request.employee_id, request.leave_type_id)][0 if request.status in _TAKEN else 1] += days
    types = await _types(session)
    by_id = {c.id: c for c in colleagues}
    return sorted(
        (
            TakenRow(by_id[employee_id], type_id, types.get(type_id, ""), values[0], values[1])
            for (employee_id, type_id), values in sums.items()
        ),
        key=lambda row: (row.colleague.name.lower(), row.leave_type_name.lower()),
    )


async def request_rows(
    session: Session, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> list[list[Cell]]:
    colleagues, _ = await people.employed_colleagues(
        session, {people.EMPLOYEE_ID_FIELD: {"$in": list(employee_ids)}}, on=end, since=start
    )
    by_id = {c.id: c for c in colleagues}
    types = await _types(session)
    requests = await session.find(
        LeaveRequest,
        {"employee_id": {"$in": list(by_id)}, "end_date": {"$gte": start}, "start_date": {"$lte": end}},
        sort=[("start_date", 1), ("id", 1)],
    )
    return [
        [
            by_id[r.employee_id].employee_code,
            by_id[r.employee_id].name,
            types.get(r.leave_type_id, ""),
            r.start_date,
            r.start_half,
            r.end_date,
            r.end_half,
            r.days_requested,
            r.status,
            r.created_at,
        ]
        for r in requests
    ]


async def balance_rows(session: Session, employee_ids: Collection[uuid.UUID], on: date) -> list[list[Cell]]:
    colleagues, _ = await people.employed_colleagues(
        session, {people.EMPLOYEE_ID_FIELD: {"$in": list(employee_ids)}}, on=on, since=EARLIEST
    )
    ids = [c.id for c in colleagues]
    if not ids:
        return []
    balances = await sums_by_type(session, LeaveLedgerEntry, "amount", {"employee_id": {"$in": ids}})
    pending = await sums_by_type(
        session,
        LeaveRequest,
        "days_requested",
        {"employee_id": {"$in": ids}, "status": RequestStatus.PENDING.value},
    )
    types = await _types(session)
    rows: list[list[Cell]] = []
    for colleague in colleagues:
        keys = sorted(
            {key for key in (*balances, *pending) if key[0] == colleague.id},
            key=lambda key: types.get(key[1], "").lower(),
        )
        for key in keys:
            balance, waiting = balances.get(key, ZERO), pending.get(key, ZERO)
            rows.append(
                [
                    colleague.employee_code,
                    colleague.name,
                    types.get(key[1], ""),
                    balance,
                    waiting,
                    balance - waiting,
                ]
            )
    return rows
