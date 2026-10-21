"""Every stored document model: the collections the migration command declares.

Used by `python -m app.cli migrate` (collections, validators, indexes), the database role
definitions (`scripts/mongo_roles.py`) and the schema tests. Modules never import each other's
models; this composition root is the one place that imports them all.
"""

from typing import Final

from app.modules.access.models import RoleGrantRequest, UserRole
from app.modules.attendance.models import (
    AttendanceCorrection,
    AttendanceCorrectionItem,
    AttendanceEvent,
    AttendanceEventVoid,
    AttendanceLock,
    AttendancePolicy,
    LocationDefaultShift,
    Shift,
    ShiftAssignment,
    ShiftVersion,
)
from app.modules.documents.models import Document, DocumentCategory, DocumentVersion
from app.modules.identity.models import (
    AuthSession,
    Credential,
    MfaFactor,
    OneTimeToken,
    RecoveryCode,
    SessionToken,
    TrustedDevice,
    User,
)
from app.modules.leave.models import (
    Holiday,
    HolidayCalendar,
    LeaveDecision,
    LeaveLedgerEntry,
    LeavePolicy,
    LeaveRequest,
    LeaveType,
    LocationHolidayCalendar,
)
from app.modules.notify.models import EmailOutbox, Notification, NotificationPreference
from app.modules.org.models import Department, Designation, Location
from app.modules.people.models import (
    EmergencyContact,
    Employee,
    EmployeeJob,
    EmployeePersonal,
    EmployeeStatusChange,
)
from app.modules.reports.models import ExportJob
from app.platform.audit.tables import (
    AuditLock,
    AuditLogRecord,
    ChainCheckpoint,
    ChainLink,
    SecurityEventRecord,
)
from app.platform.db import Document as BaseDocument
from app.platform.db import NamedLock
from app.platform.ratelimit import RateLimitCounter
from app.platform.settings import SettingOverride
from app.platform.tenancy import Organization

MODELS: Final[tuple[type[BaseDocument], ...]] = (
    Organization,
    SettingOverride,
    NamedLock,
    RateLimitCounter,
    # audit
    AuditLogRecord,
    SecurityEventRecord,
    ChainLink,
    ChainCheckpoint,
    AuditLock,
    # identity
    User,
    Credential,
    MfaFactor,
    RecoveryCode,
    AuthSession,
    SessionToken,
    OneTimeToken,
    TrustedDevice,
    # access
    UserRole,
    RoleGrantRequest,
    # org and people
    Location,
    Department,
    Designation,
    Employee,
    EmployeeJob,
    EmployeePersonal,
    EmergencyContact,
    EmployeeStatusChange,
    # attendance
    AttendanceEvent,
    Shift,
    ShiftVersion,
    LocationDefaultShift,
    ShiftAssignment,
    AttendancePolicy,
    AttendanceCorrection,
    AttendanceCorrectionItem,
    AttendanceEventVoid,
    AttendanceLock,
    # leave
    HolidayCalendar,
    Holiday,
    LocationHolidayCalendar,
    LeaveType,
    LeavePolicy,
    LeaveRequest,
    LeaveDecision,
    LeaveLedgerEntry,
    # notify
    EmailOutbox,
    Notification,
    NotificationPreference,
    # documents and reports
    DocumentCategory,
    Document,
    DocumentVersion,
    ExportJob,
)
