"""What the database and the unit of work guarantee whatever a service does (ADR-030):

- **Least privilege.** Each process's database user can do only what that process needs: the
  API cannot change or remove audit records, attendance events, the leave ledger or histories,
  cannot touch the audit chain, cannot create or drop collections; only the retention user can
  remove audit records (docs/security-architecture.md §6.4, §8).
- **Validators.** Every collection refuses a document with a missing, extra or wrongly typed
  field, a value outside its allowed set, or a broken rule across its fields (formerly CHECK
  constraints).
- **Uniqueness** per organization, and across organizations only where the design says so.
- **Update rules** (formerly guard triggers): append-only collections, set-once fields,
  allowed status moves.
- **Tenant isolation.** A unit of work reads and writes only its own organization's documents.
- **The MFA rule** across collections, checked before every commit that could break it.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pymongo.errors import OperationFailure, WriteError

from app.metadata import MODELS
from app.modules.access.models import GrantKind, GrantStatus, RoleGrantRequest, role_id
from app.modules.attendance.models import AttendanceEvent
from app.modules.identity.invariants import MfaInvariantError, require_mfa
from app.modules.identity.models import FactorType, MfaFactor, User, UserStatus
from app.modules.leave.models import LeaveLedgerEntry
from app.modules.org.models import Department, Location
from app.modules.people.models import Employee, EmployeeJob, EmployeeStatusChange
from app.platform.audit.tables import AuditLogRecord, ChainLink
from app.platform.config import PRODUCTION_DATABASE
from app.platform.db import (
    ID,
    AppendOnlyError,
    ConcurrentChangeError,
    Database,
    GuardedUpdateError,
    TenantScopeError,
    UniqueViolationError,
    create_client,
    utcnow,
)
from app.platform.files import FILE_INDEXES
from app.platform.ids import uuid7
from app.platform.ratelimit import RateLimitCounter
from app.platform.schema import migrate
from migrations import MIGRATIONS
from tests.conftest import DATABASE_NAME, MongoServer, database_as, fresh_migrated_database
from tests.people_data import add_job, create_employee, create_org_units, fake_code

UNAUTHORIZED = 13
VALIDATION_FAILED = 121


@pytest.fixture
async def app_db(migrated_mongo: MongoServer) -> AsyncIterator[Database]:
    async with database_as(migrated_mongo, "hrms_app") as database:
        yield database


@pytest.fixture
def organization() -> uuid.UUID:
    return uuid7()


def raw(collection: str, organization: uuid.UUID, **fields: Any) -> dict[str, Any]:
    return {ID: uuid7(), "organization_id": organization, **fields}


# --- least privilege ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "collection",
    [
        AuditLogRecord.__collection__,
        "security_events",
        AttendanceEvent.__collection__,
        LeaveLedgerEntry.__collection__,
        EmployeeStatusChange.__collection__,
        "leave_decisions",
        "attendance_correction_items",
        "attendance_event_voids",
    ],
)
async def test_the_api_cannot_change_or_remove_append_only_records(app_db: Database, collection: str) -> None:
    target = app_db.collection(collection)
    with pytest.raises(OperationFailure) as updating:
        await target.update_many({}, {"$set": {"tampered": True}})
    assert updating.value.code == UNAUTHORIZED
    with pytest.raises(OperationFailure) as removing:
        await target.delete_many({})
    assert removing.value.code == UNAUTHORIZED


@pytest.mark.parametrize(
    "collection",
    [ChainLink.__collection__, "audit_checkpoints", "audit_locks", "job_leases", "schema_migrations"],
)
async def test_the_api_cannot_touch_the_audit_chain_jobs_or_migrations(
    app_db: Database, collection: str
) -> None:
    with pytest.raises(OperationFailure) as reading:
        await app_db.collection(collection).find_one({})
    assert reading.value.code == UNAUTHORIZED


async def test_the_api_cannot_create_drop_or_change_collections(app_db: Database) -> None:
    for action in (
        app_db.db.create_collection(f"rogue_{secrets.token_hex(3)}"),
        app_db.db.drop_collection("employees"),
        app_db.db.command({"collMod": "employees", "validator": {}}),
        app_db.collection("employees").create_index("legal_first_name"),
        app_db.collection("organizations").delete_many({}),
    ):
        with pytest.raises(OperationFailure) as refused:
            await action
        assert refused.value.code == UNAUTHORIZED


async def test_only_the_retention_user_removes_audit_records(migrated_mongo: MongoServer) -> None:
    async with database_as(migrated_mongo, "hrms_worker") as worker:
        with pytest.raises(OperationFailure):
            await worker.collection(AuditLogRecord.__collection__).delete_many({"month": "1999-01"})
        with pytest.raises(OperationFailure):
            await worker.collection(ChainLink.__collection__).delete_many({"partition_key": "1999-01"})
    async with database_as(migrated_mongo, "hrms_audit_retention") as retention:
        await retention.collection(AuditLogRecord.__collection__).delete_many({"month": "1999-01"})
        for collection in ("employees", "users", "leave_ledger"):
            with pytest.raises(OperationFailure):
                await retention.collection(collection).find_one({})
        with pytest.raises(OperationFailure):
            await retention.collection(AuditLogRecord.__collection__).update_many({}, {"$set": {"x": 1}})


# --- validators ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("collection", "document"),
    [
        # A missing field, an extra field, a wrong type.
        ("locations", {"code": "X", "name": "X", "time_zone": "UTC", "country_code": "IN"}),
        (
            "departments",
            {
                "code": "X",
                "name": "X",
                "description": None,
                "parent_id": None,
                "head_employee_id": None,
                "status": "active",
                "version": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "unexpected": 1,
            },
        ),
        # A value outside its allowed set or pattern.
        (
            "locations",
            {
                "code": "X",
                "name": "X",
                "time_zone": "UTC",
                "country_code": "india",
                "address": None,
                "status": "active",
                "version": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        ),
        (
            "locations",
            {
                "code": "X",
                "name": "X",
                "time_zone": "UTC",
                "country_code": "IN",
                "address": None,
                "status": "deleted",
                "version": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        ),
    ],
)
async def test_validators_refuse_malformed_documents(
    app_db: Database, organization: uuid.UUID, collection: str, document: dict[str, Any]
) -> None:
    with pytest.raises(WriteError) as refused:
        await app_db.collection(collection).insert_one(raw(collection, organization, **document))
    assert refused.value.code == VALIDATION_FAILED


async def test_rules_within_one_document_are_enforced_by_the_database(
    app_db: Database, organization: uuid.UUID
) -> None:
    async with app_db.unit_of_work(tenant=organization) as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=date(2026, 1, 1))
    refused = [
        # An exit before joining; an exited record without an exit date.
        Employee(
            employee_code=fake_code(),
            legal_first_name="Test",
            date_of_joining=date(2026, 5, 1),
            date_of_exit=date(2026, 4, 1),
            status="exited",
        ),
        Employee(
            employee_code=fake_code(),
            legal_first_name="Test",
            date_of_joining=date(2026, 5, 1),
            status="exited",
        ),
        # An empty job range; a person managing themselves.
        EmployeeJob(
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 3, 1),
            department_id=units.department_id,
            designation_id=units.designation_id,
            location_id=units.location_id,
            employment_type="full_time",
            change_reason="joining",
        ),
        EmployeeJob(
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            department_id=units.department_id,
            designation_id=units.designation_id,
            location_id=units.location_id,
            manager_employee_id=employee_id,
            employment_type="full_time",
            change_reason="joining",
        ),
        # A ledger usage entry must be negative and name its request.
        LeaveLedgerEntry(
            employee_id=employee_id,
            leave_type_id=uuid7(),
            leave_year=date(2026, 1, 1),
            entry_type="usage",
            amount=Decimal("1.00"),
            effective_date=date(2026, 3, 2),
        ),
        # An active user must have verified their email.
        User(email=f"{secrets.token_hex(4)}@dev.example", status=UserStatus.ACTIVE.value),
    ]
    for document in refused:
        with pytest.raises(WriteError) as error:
            async with app_db.unit_of_work(tenant=organization) as session:
                session.add(document)
        assert error.value.code == VALIDATION_FAILED, type(document).__name__


async def test_separation_of_duties_on_grant_requests_is_a_database_rule(
    app_db: Database, organization: uuid.UUID
) -> None:
    person = uuid7()
    base: dict[str, Any] = {
        "subject_user_id": person,
        "role_id": role_id("hr"),
        "role_key": "hr",
        "reason": "Cover",
        "requested_by": person,
        "requested_duration": timedelta(hours=1),
    }
    refused = [
        # The requester decides their own elevation.
        RoleGrantRequest(
            kind=GrantKind.ELEVATION.value,
            status=GrantStatus.REJECTED.value,
            decided_by=person,
            decided_at=utcnow(),
            **base,
        ),
        # An elevation longer than eight hours.
        RoleGrantRequest(
            kind=GrantKind.ELEVATION.value,
            status=GrantStatus.PENDING.value,
            **(base | {"requested_duration": timedelta(hours=9)}),
        ),
        # A data role through break-glass, which reaches only system_admin.
        RoleGrantRequest(kind=GrantKind.BREAK_GLASS.value, status=GrantStatus.ACTIVE.value, **base),
    ]
    for request in refused:
        with pytest.raises(WriteError) as error:
            async with app_db.unit_of_work(tenant=organization) as session:
                session.add(request)
        assert error.value.code == VALIDATION_FAILED


# --- uniqueness ---------------------------------------------------------------------------------


async def test_codes_are_unique_per_organization_regardless_of_case(app_db: Database) -> None:
    first, second = uuid7(), uuid7()
    code = fake_code("DEPT")
    async with app_db.unit_of_work(tenant=first) as session:
        session.add(Department(code=code, name="Test"))
    with pytest.raises(UniqueViolationError) as duplicate:
        async with app_db.unit_of_work(tenant=first) as session:
            session.add(Department(code=code.lower(), name="Test"))
    assert duplicate.value.index == "uq_departments_code"
    # Another organization may use the same code.
    async with app_db.unit_of_work(tenant=second) as session:
        session.add(Department(code=code, name="Test"))


async def test_an_email_address_has_one_account_across_organizations(app_db: Database) -> None:
    email = f"{secrets.token_hex(5)}@dev.example"
    async with app_db.unit_of_work(tenant=uuid7()) as session:
        session.add(User(email=email, status=UserStatus.INVITED.value))
    with pytest.raises(UniqueViolationError) as duplicate:
        async with app_db.unit_of_work(tenant=uuid7()) as session:
            session.add(User(email=email.upper(), status=UserStatus.INVITED.value))
    assert duplicate.value.index == "uq_users_email"


# --- update rules -------------------------------------------------------------------------------


async def test_append_only_documents_never_change(app_db: Database, organization: uuid.UUID) -> None:
    record = AuditLogRecord(
        occurred_at=utcnow(),
        actor_type="system",
        action="test.created",
        outcome="success",
        month=utcnow().strftime("%Y-%m"),
    )
    async with app_db.unit_of_work(tenant=organization) as session:
        session.add(record)
    with pytest.raises(AppendOnlyError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            loaded = await session.get(AuditLogRecord, record.id)
            assert loaded is not None
            loaded.action = "test.rewritten"
    with pytest.raises(AppendOnlyError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            loaded = await session.get(AuditLogRecord, record.id)
            assert loaded is not None
            await session.delete(loaded)


async def test_closing_a_job_row_is_the_only_change_it_allows(
    app_db: Database, organization: uuid.UUID
) -> None:
    async with app_db.unit_of_work(tenant=organization) as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=date(2026, 1, 1))
        job_id = await add_job(session, units, employee_id, manager_id=None, effective_from=date(2026, 1, 1))
    with pytest.raises(GuardedUpdateError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            job = await session.get(EmployeeJob, job_id)
            assert job is not None
            job.department_id = uuid7()
    async with app_db.unit_of_work(tenant=organization) as session:
        job = await session.get(EmployeeJob, job_id)
        assert job is not None
        job.effective_to = date(2026, 6, 1)
    with pytest.raises(AppendOnlyError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            job = await session.get(EmployeeJob, job_id)
            assert job is not None
            await session.delete(job)


async def test_a_grant_request_status_only_moves_forward(app_db: Database, organization: uuid.UUID) -> None:
    subject, admin = uuid7(), uuid7()
    request = RoleGrantRequest(
        kind=GrantKind.SUPER_ADMIN_ASSIGNMENT.value,
        subject_user_id=subject,
        role_id=role_id("super_admin"),
        role_key="super_admin",
        reason="Second admin",
        requested_by=admin,
        status=GrantStatus.PENDING.value,
    )
    async with app_db.unit_of_work(tenant=organization) as session:
        session.add(request)
    async with app_db.unit_of_work(tenant=organization) as session:
        loaded = await session.get(RoleGrantRequest, request.id)
        assert loaded is not None
        loaded.status, loaded.revoked_at, loaded.revoked_by = GrantStatus.REVOKED.value, utcnow(), admin
    with pytest.raises(GuardedUpdateError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            loaded = await session.get(RoleGrantRequest, request.id)
            assert loaded is not None
            loaded.status = GrantStatus.PENDING.value
    with pytest.raises(GuardedUpdateError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            loaded = await session.get(RoleGrantRequest, request.id)
            assert loaded is not None
            loaded.reason = "Rewritten"


# --- tenant isolation ---------------------------------------------------------------------------


async def test_a_unit_of_work_sees_only_its_own_organization(app_db: Database) -> None:
    ours, theirs = uuid7(), uuid7()
    location = Location(code=fake_code(), name="Theirs", time_zone="UTC", country_code="IN")
    async with app_db.unit_of_work(tenant=theirs) as session:
        session.add(location)
    async with app_db.unit_of_work(tenant=ours) as session:
        assert await session.get(Location, location.id) is None
        assert await session.find(Location, {"id": location.id}) == []
        assert await session.count(Location, {"id": location.id}) == 0
        assert await session.update_many(Location, {"id": location.id}, {"$set": {"name": "Taken"}}) == 0
        with pytest.raises(TenantScopeError):
            await session.find(Location, {"organization_id": theirs})
        with pytest.raises(TenantScopeError):
            session.add(
                Location(
                    organization_id=theirs, code=fake_code(), name="X", time_zone="UTC", country_code="IN"
                )
            )
    async with app_db.unit_of_work() as session:
        with pytest.raises(TenantScopeError):
            await session.find(Location, {})


async def test_a_pipeline_reads_other_tenant_owned_collections_only_inside_the_organization(
    app_db: Database,
) -> None:
    ours, theirs = uuid7(), uuid7()
    their_location = Location(code=fake_code(), name="Theirs", time_zone="UTC", country_code="IN")
    our_location = Location(code=fake_code(), name="Ours", time_zone="UTC", country_code="IN")
    async with app_db.unit_of_work(tenant=theirs) as session:
        session.add(their_location)
    async with app_db.unit_of_work(tenant=ours) as session:
        session.add(our_location)
    locations = Location.__collection__
    everything = {"$expr": {"$eq": [1, 1]}}
    async with app_db.unit_of_work(tenant=ours) as session:
        joined = await session.aggregate(
            Location, [session.tenant_lookup(Location, local="code", foreign="code", into="same")]
        )
        assert [len(row["same"]) for row in joined] == [1]
        refused: list[dict[str, Any]] = [
            {"$lookup": {"from": locations, "pipeline": [{"$match": everything}], "as": "all"}},
            {"$lookup": {"from": locations, "localField": "code", "foreignField": "code", "as": "all"}},
            {
                "$lookup": {
                    "from": locations,
                    "pipeline": [{"$match": {"organization_id": theirs}}],
                    "as": "x",
                }
            },
            {"$unionWith": locations},
            {"$unionWith": {"coll": locations}},
            {
                "$graphLookup": {
                    "from": locations,
                    "startWith": "$code",
                    "connectFromField": "code",
                    "connectToField": "code",
                    "as": "all",
                }
            },
            {"$facet": {"all": [{"$unionWith": locations}]}},
            {
                "$lookup": {
                    "from": RateLimitCounter.__collection__,
                    "pipeline": [{"$unionWith": locations}],
                    "as": "all",
                }
            },
            {"$out": "copied"},
            {"$merge": {"into": "copied"}},
        ]
        for stage in refused:
            with pytest.raises(TenantScopeError):
                await session.aggregate(Location, [stage])
        # A collection no organization owns may be joined as it is.
        counters = await session.aggregate(
            Location,
            [{"$lookup": {"from": RateLimitCounter.__collection__, "pipeline": [{"$limit": 1}], "as": "c"}}],
        )
        assert len(counters) == 1


async def test_reads_named_per_organization_are_refused_outside_one(app_db: Database) -> None:
    organization = uuid7()
    async with app_db.unit_of_work(tenant=organization) as session:
        assert session.require_tenant() == organization
    async with app_db.unit_of_work() as session:
        with pytest.raises(TenantScopeError):
            session.require_tenant()


# --- the MFA rule -------------------------------------------------------------------------------


async def test_an_active_account_without_a_factor_never_commits(
    app_db: Database, organization: uuid.UUID
) -> None:
    user = User(
        email=f"{secrets.token_hex(5)}@dev.example",
        status=UserStatus.ACTIVE.value,
        email_verified_at=utcnow(),
    )
    with pytest.raises(MfaInvariantError):  # noqa: PT012 (the commit raises)
        async with app_db.unit_of_work(tenant=organization) as session:
            session.add(user)
            require_mfa(session, user.id)
    async with app_db.unit_of_work(tenant=organization) as session:
        assert await session.get(User, user.id) is None


async def test_removing_two_factors_at_once_cannot_leave_an_account_without_mfa(
    app_db: Database, organization: uuid.UUID
) -> None:
    """Write skew: each transaction removes a different factor and still sees the other one. The
    check locks the account, so the second to finish conflicts instead of committing."""
    user = User(
        email=f"{secrets.token_hex(5)}@dev.example",
        status=UserStatus.ACTIVE.value,
        email_verified_at=utcnow(),
    )
    factors = [
        MfaFactor(
            user_id=user.id,
            type=FactorType.TOTP.value,
            label=f"Phone {n}",
            secret_ciphertext=b"ciphertext",
            secret_key_version=1,
            confirmed_at=utcnow(),
        )
        for n in (1, 2)
    ]
    async with app_db.unit_of_work(tenant=organization) as session:
        session.add(user)
        for factor in factors:
            session.add(factor)
        require_mfa(session, user.id)

    first_work, second_work = (
        app_db.unit_of_work(tenant=organization),
        app_db.unit_of_work(tenant=organization),
    )
    first, second = await first_work.__aenter__(), await second_work.__aenter__()
    for session, factor in ((first, factors[0]), (second, factors[1])):
        stored = await session.get(MfaFactor, factor.id)
        assert stored is not None
        stored.revoked_at = utcnow()
        await session.flush()
        require_mfa(session, user.id)
    await first_work.__aexit__(None, None, None)
    with pytest.raises(ConcurrentChangeError):
        await second_work.__aexit__(None, None, None)

    async with app_db.unit_of_work(tenant=organization) as session:
        left = await session.find(MfaFactor, {"user_id": user.id, "revoked_at": None})
        assert [factor.id for factor in left] == [factors[1].id]


def test_the_tests_never_use_the_production_database_name() -> None:
    assert DATABASE_NAME != PRODUCTION_DATABASE


async def test_migrating_again_changes_nothing(migrated_mongo: MongoServer) -> None:
    name = await fresh_migrated_database(migrated_mongo)
    database = Database(
        create_client(migrated_mongo.uri("hrms_migrator"), application_name="test-migrate"), name
    )
    try:
        report = await migrate(database, MODELS, MIGRATIONS, FILE_INDEXES)
    finally:
        await database.dispose()
    assert report.created_collections == []
    assert report.created_indexes == []
    assert report.applied_versions == []
    assert report.undeclared_collections == []
    assert report.undeclared_indexes == []
