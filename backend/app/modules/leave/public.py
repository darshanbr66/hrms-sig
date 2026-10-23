"""The leave module's interface for other modules and the worker (docs/architecture.md §4).

- `LeaveEmails` renders the leave request email at send time (notify.public.Renderer).
- `erase_employee` is the leave part of the permanent erasure of one person's data.
- `register_leave_jobs` adds the daily periodic accrual and carried-day expiry to the worker.
- `request_rows`, `balance_rows` and their headers give the export job the leave rows.
- `holiday_dates`, `working_day_dates` and `leave_portions` give the attendance module the
  holidays and declared working days of each location and the days covered by approved
  leave, as dates and portions only.
"""

from app.modules.leave.calendar import holiday_dates, leave_portions, working_day_dates
from app.modules.leave.emails import LeaveEmails
from app.modules.leave.erasure import erase_employee
from app.modules.leave.reports import BALANCES_HEADER, REQUESTS_HEADER, balance_rows, request_rows
from app.modules.leave.year import register_leave_jobs

__all__ = [
    "BALANCES_HEADER",
    "REQUESTS_HEADER",
    "LeaveEmails",
    "balance_rows",
    "erase_employee",
    "holiday_dates",
    "leave_portions",
    "register_leave_jobs",
    "request_rows",
    "working_day_dates",
]
