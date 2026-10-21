"""Periodic accrual, proration, the year end, carried-day expiry and opening-balance imports
(product-requirements LV-1, LV-2, §6.12). Every rule and amount is an obviously fake test value."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.modules.leave.models import LeaveLedgerEntry
from app.modules.leave.year import accrue_due
from app.modules.people.models import Employee
from app.platform.audit.writer import AuditWriter
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units, fake_code


@dataclass
class Setup:
    units: OrgUnits
    admin: Account
    employee_id: str
    employee_code: str
    leave_type_id: str
    leave_type_code: str


async def setup(api: Api, joined: date, **policy: Any) -> Setup:
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee = await create_employee(session, joined=joined)
        await add_job(session, units, employee, manager_id=None, effective_from=joined)
    [stored] = await api.records(Employee, {"id": employee})
    code = stored.employee_code
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    type_code = fake_code("LT")
    async with api.client() as client:
        await api.sign_in(client, admin)
        leave_type = await client.post(
            "/api/v1/leave-types",
            json={
                "code": type_code,
                "name": "Test accruing leave",
                "is_paid": True,
                "requires_approval": True,
                "allows_half_day": True,
                "counts_weekly_offs": False,
                "counts_holidays": False,
            },
        )
        assert leave_type.status_code == 201, leave_type.text
        body = {
            "leave_type_id": leave_type.json()["id"],
            "location_id": str(units.location_id),
            "effective_from": "2020-01-01",
            "leave_year_start_month": 1,
            "leave_year_start_day": 1,
            "allow_negative_balance": False,
            **policy,
        }
        created = await client.post("/api/v1/leave-policies", json=body)
        assert created.status_code == 201, created.text
    return Setup(units, admin, str(employee), code, leave_type.json()["id"], type_code)


async def ledger(api: Api, s: Setup) -> list[tuple[str, Decimal]]:
    rows = await api.records(
        LeaveLedgerEntry,
        {"employee_id": uuid.UUID(s.employee_id)},
        sort=[("effective_date", 1), ("id", 1)],
    )
    return [(row.entry_type, row.amount) for row in rows]


async def test_periodic_accrual_with_proration_runs_once_per_period(api: Api) -> None:
    today = api.clock.now().date()
    joined = date(today.year, 1, 16) if today.month > 2 else date(today.year - 1, 11, 16)
    s = await setup(
        api,
        joined,
        accrual_method="periodic",
        accrual_amount="1.75",
        accrual_frequency="monthly",
        accrual_timing="period_end",
        prorate_on_joining=True,
        prorate_on_exit=False,
        proration_rounding="nearest_half",
    )
    first = await accrue_due(api.worker_database, api.clock, AuditWriter(api.clock))
    assert first.accrued >= 1
    entries = await ledger(api, s)
    assert all(kind == "accrual" for kind, _ in entries)
    # The joining month is prorated (about half of 1.75 days, to the nearest half day).
    assert entries[0][1] == Decimal("1.00")
    assert all(amount == Decimal("1.75") for _, amount in entries[1:])
    again = await accrue_due(api.worker_database, api.clock, AuditWriter(api.clock))
    assert await ledger(api, s) == entries
    assert again.accrued == 0


async def test_new_policies_must_decide_every_rule_their_accrual_needs(api: Api) -> None:
    s = await setup(api, date(2024, 1, 1), accrual_method="none")
    async with api.client() as client:
        await api.sign_in(client, s.admin)
        base: dict[str, object] = {
            "leave_type_id": s.leave_type_id,
            "effective_from": "2031-01-01",
            "leave_year_start_month": 1,
            "leave_year_start_day": 1,
            "allow_negative_balance": False,
        }
        cases: list[tuple[dict[str, object], str]] = [
            ({"accrual_method": "periodic", "accrual_amount": "1"}, "leave.accrual_period"),
            (
                {"accrual_method": "upfront_per_leave_year", "accrual_amount": "12"},
                "leave.proration_undecided",
            ),
            (
                {
                    "accrual_method": "upfront_per_leave_year",
                    "accrual_amount": "12",
                    "prorate_on_joining": True,
                },
                "leave.proration_rounding",
            ),
            ({"accrual_method": "none", "carry_forward_mode": "capped"}, "leave.carry_forward_cap"),
            (
                {"accrual_method": "none", "carry_forward_mode": "capped", "carry_forward_cap": "5"},
                "leave.excess_action",
            ),
        ]
        for extra, code in cases:
            refused = await client.post("/api/v1/leave-policies", json=base | extra)
            assert refused.status_code == 422, refused.text
            assert refused.json()["errors"][0]["code"] == code


async def year_end(client: httpx.AsyncClient, s: Setup, last_day: str, *, apply: bool) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/leave-year-end",
        json={"leave_type_id": s.leave_type_id, "year_end": last_day, "apply": apply},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_the_year_end_carries_up_to_the_cap_and_lapses_the_rest_once(api: Api) -> None:
    s = await setup(
        api,
        date(2023, 1, 1),
        accrual_method="none",
        carry_forward_mode="capped",
        carry_forward_cap="5",
        excess_action="lapse",
        carried_expire_after_months=3,
    )
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        imported = await client.post(
            "/api/v1/leave-opening-balances",
            json={
                "reason": "Test: balances from the old system",
                "apply": True,
                "rows": [
                    {
                        "employee_code": s.employee_code,
                        "leave_type_code": s.leave_type_code,
                        "days": "12.5",
                        "effective_date": "2024-01-01",
                    },
                    {
                        "employee_code": "TEST-NOBODY",
                        "leave_type_code": s.leave_type_code,
                        "days": "1",
                        "effective_date": "2024-01-01",
                    },
                ],
            },
        )
        # One bad row: nothing is recorded.
        assert imported.json()["applied"] is False
        assert [row["outcome"] for row in imported.json()["items"]] == ["ok", "unknown_employee"]
        good = await client.post(
            "/api/v1/leave-opening-balances",
            json={
                "reason": "Test: balances from the old system",
                "apply": True,
                "rows": [
                    {
                        "employee_code": s.employee_code,
                        "leave_type_code": s.leave_type_code,
                        "days": "12.5",
                        "effective_date": "2024-01-01",
                    }
                ],
            },
        )
        assert good.json()["applied"] is True
        assert good.json()["counts"] == {"recorded": 1}

        preview = await year_end(client, s, "2024-12-31", apply=False)
        row = next(item for item in preview["items"] if item["employee"]["id"] == s.employee_id)
        assert row["outcome"] == "would_do"
        assert (row["carried"], row["lapsed"]) == ("5.00", "7.50")
        assert await ledger(api, s) == [("opening_balance", Decimal("12.50"))]
        applied = await year_end(client, s, "2024-12-31", apply=True)
        assert next(i for i in applied["items"] if i["employee"]["id"] == s.employee_id)["outcome"] == "done"
        again = await year_end(client, s, "2024-12-31", apply=True)
        assert (
            next(i for i in again["items"] if i["employee"]["id"] == s.employee_id)["outcome"]
            == "already_closed"
        )
        wrong_day = await year_end(client, s, "2024-06-30", apply=False)
        assert (
            next(i for i in wrong_day["items"] if i["employee"]["id"] == s.employee_id)["outcome"]
            == "not_year_end"
        )
    assert sorted(await ledger(api, s)) == sorted(
        [
            ("opening_balance", Decimal("12.50")),
            ("carry_forward_out", Decimal("-5.00")),
            ("lapse", Decimal("-7.50")),
            ("carry_forward", Decimal("5.00")),
        ]
    )
    # Three months into 2025 the unused carried days expire, once.
    await accrue_due(api.worker_database, api.clock, AuditWriter(api.clock))
    await accrue_due(api.worker_database, api.clock, AuditWriter(api.clock))
    expiries = [amount for kind, amount in await ledger(api, s) if kind == "carry_forward_expiry"]
    assert expiries == [Decimal("-5.00")]


async def test_weekly_offs_of_the_shift_are_not_counted_when_the_type_says_so(api: Api) -> None:
    s = await setup(
        api,
        date(2024, 1, 1),
        accrual_method="upfront_per_leave_year",
        accrual_amount="20",
        prorate_on_joining=False,
    )
    async with api.client() as client:
        await api.sign_in(client, s.admin)
        shift = await client.post(
            "/api/v1/shifts",
            json={
                "code": fake_code("SH"),
                "name": "Test weekday shift",
                "version": {
                    "effective_from": "2024-01-01",
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "weekly_offs": [{"weekday": 6}, {"weekday": 7}],
                },
            },
        )
        await client.post(
            "/api/v1/location-default-shifts",
            json={
                "location_id": str(s.units.location_id),
                "shift_id": shift.json()["id"],
                "effective_from": "2024-01-01",
            },
        )
    employee = await api.create_account(employee_id=uuid.UUID(s.employee_id))
    today = api.clock.now().date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    async with api.client() as client:
        await api.sign_in(client, employee)
        preview = await client.post(
            "/api/v1/me/leave-requests/preview",
            json={
                "leave_type_id": s.leave_type_id,
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=7)).isoformat(),
            },
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["days_requested"] == "6.00"
        assert [d["not_counted_reason"] for d in body["days"] if not d["counted"]] == [
            "weekly_off",
            "weekly_off",
        ]
