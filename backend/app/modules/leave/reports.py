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
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leave.models import LeaveLedgerEntry, LeaveRequest, LeaveRequestDay, LeaveType, RequestStatus
from app.modules.people import public as people
from app.platform.csvfile import Cell

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


async def _types(session: AsyncSession) -> dict[uuid.UUID, str]:
    return dict((await session.execute(select(LeaveType.id, LeaveType.name))).all())


async def taken(
    session: AsyncSession, colleagues: list[people.Colleague], start: date, end: date
) -> list[TakenRow]:
    ids = [c.id for c in colleagues]
    if not ids:
        return []
    rows = await session.execute(
        select(
            LeaveRequest.employee_id,
            LeaveRequest.leave_type_id,
            LeaveRequest.status,
            func.sum(LeaveRequestDay.portion),
        )
        .join(LeaveRequestDay, LeaveRequestDay.leave_request_id == LeaveRequest.id)
        .where(
            LeaveRequest.employee_id.in_(ids),
            LeaveRequest.status.in_([*_TAKEN, RequestStatus.PENDING.value]),
            LeaveRequestDay.counted.is_(True),
            LeaveRequestDay.date >= start,
            LeaveRequestDay.date <= end,
        )
        .group_by(LeaveRequest.employee_id, LeaveRequest.leave_type_id, LeaveRequest.status)
    )
    sums: dict[tuple[uuid.UUID, uuid.UUID], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for employee_id, type_id, status, days in rows.all():
        sums[(employee_id, type_id)][0 if status in _TAKEN else 1] += Decimal(days)
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
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> list[list[Cell]]:
    colleagues, _ = await people.employed_colleagues(
        session, people.employee_id_column().in_(list(employee_ids)), on=end, since=start
    )
    by_id = {c.id: c for c in colleagues}
    types = await _types(session)
    requests = await session.scalars(
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id.in_(list(by_id)),
            LeaveRequest.end_date >= start,
            LeaveRequest.start_date <= end,
        )
        .order_by(LeaveRequest.start_date, LeaveRequest.id)
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


async def balance_rows(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], on: date
) -> list[list[Cell]]:
    colleagues, _ = await people.employed_colleagues(
        session, people.employee_id_column().in_(list(employee_ids)), on=on, since=EARLIEST
    )
    ids = [c.id for c in colleagues]
    if not ids:
        return []
    balances = {
        (row[0], row[1]): Decimal(row[2])
        for row in (
            await session.execute(
                select(
                    LeaveLedgerEntry.employee_id,
                    LeaveLedgerEntry.leave_type_id,
                    func.sum(LeaveLedgerEntry.amount),
                )
                .where(LeaveLedgerEntry.employee_id.in_(ids))
                .group_by(LeaveLedgerEntry.employee_id, LeaveLedgerEntry.leave_type_id)
            )
        ).all()
    }
    pending = {
        (row[0], row[1]): Decimal(row[2])
        for row in (
            await session.execute(
                select(
                    LeaveRequest.employee_id,
                    LeaveRequest.leave_type_id,
                    func.sum(LeaveRequest.days_requested),
                )
                .where(LeaveRequest.employee_id.in_(ids), LeaveRequest.status == RequestStatus.PENDING.value)
                .group_by(LeaveRequest.employee_id, LeaveRequest.leave_type_id)
            )
        ).all()
    }
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
