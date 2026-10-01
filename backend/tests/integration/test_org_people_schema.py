"""Constraints and the job-history guard on the org and people tables (revisions 0003, 0004).

Written as raw SQL against the application role, so each test proves the database rule on
its own, independent of any Python validation.
"""

import re
import uuid
from collections.abc import Iterator
from enum import StrEnum

import psycopg
import pytest
from psycopg import errors

from app.modules.org.models import OrgUnitStatus
from app.modules.people.models import EmployeeStatus, EmploymentType, JobChangeReason
from tests.conftest import PostgresServer
from tests.people_data import fake_code


@pytest.fixture
def app_connection(migrated_postgres: PostgresServer) -> Iterator[psycopg.Connection]:
    with migrated_postgres.connect("hrms_app") as connection:
        yield connection


def insert_location(connection: psycopg.Connection, **overrides: object) -> uuid.UUID:
    values: dict[str, object] = {
        "code": fake_code(),
        "name": "Test location",
        "time_zone": "Asia/Kolkata",
        "country_code": "IN",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({name})s" for name in values)
    row = connection.execute(
        f"INSERT INTO org.locations ({columns}) VALUES ({placeholders}) RETURNING id",
        values,
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def insert_named(connection: psycopg.Connection, table: str) -> uuid.UUID:
    row = connection.execute(
        f"INSERT INTO org.{table} (code, name) VALUES (%s, 'Test') RETURNING id",
        (fake_code(),),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def insert_employee(
    connection: psycopg.Connection,
    *,
    code: str | None = None,
    email: str | None = None,
    joined: str = "2030-01-01",
    exited: str | None = None,
    status: str = "active",
) -> uuid.UUID:
    row = connection.execute(
        "INSERT INTO people.employees (employee_code, legal_first_name, work_email, date_of_joining, "
        "date_of_exit, status) VALUES (%s, 'Test', %s, %s, %s, %s) RETURNING id",
        (code or fake_code(), email, joined, exited, status),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


class Org:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.location = insert_location(connection)
        self.department = insert_named(connection, "departments")
        self.designation = insert_named(connection, "designations")


def insert_job(
    connection: psycopg.Connection,
    org: Org,
    employee: uuid.UUID,
    effective_from: str,
    *,
    effective_to: str | None = None,
    manager: uuid.UUID | None = None,
    employment_type: str = "full_time",
) -> uuid.UUID:
    row = connection.execute(
        "INSERT INTO people.employee_jobs (employee_id, effective_from, effective_to, department_id, "
        "designation_id, location_id, manager_employee_id, employment_type, change_reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'joining') RETURNING id",
        (
            employee,
            effective_from,
            effective_to,
            org.department,
            org.designation,
            org.location,
            manager,
            employment_type,
        ),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def test_codes_are_unique_regardless_of_case(app_connection: psycopg.Connection) -> None:
    code = fake_code()
    insert_location(app_connection, code=code)
    with pytest.raises(errors.UniqueViolation):
        insert_location(app_connection, code=code.lower())
    employee_code = fake_code()
    insert_employee(app_connection, code=employee_code)
    with pytest.raises(errors.UniqueViolation):
        insert_employee(app_connection, code=employee_code.lower())


def test_work_email_is_unique_regardless_of_case(app_connection: psycopg.Connection) -> None:
    email = f"{fake_code().lower()}@dev.example"
    insert_employee(app_connection, email=email)
    with pytest.raises(errors.UniqueViolation):
        insert_employee(app_connection, email=email.upper())


@pytest.mark.parametrize(
    "overrides",
    [
        {"country_code": "in"},
        {"country_code": "IND"},
        {"status": "closed"},
        {"name": ""},
        {"name": "x" * 201},
        {"code": "x" * 33},
        {"time_zone": ""},
        {"address": "x" * 1001},
    ],
)
def test_location_values_are_checked(
    app_connection: psycopg.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises((errors.CheckViolation, errors.StringDataRightTruncation)):
        insert_location(app_connection, **overrides)


def test_department_cannot_be_its_own_parent(app_connection: psycopg.Connection) -> None:
    department = insert_named(app_connection, "departments")
    with pytest.raises(errors.CheckViolation):
        app_connection.execute("UPDATE org.departments SET parent_id = id WHERE id = %s", (department,))


def test_department_head_must_be_an_employee(app_connection: psycopg.Connection) -> None:
    department = insert_named(app_connection, "departments")
    with pytest.raises(errors.ForeignKeyViolation):
        app_connection.execute(
            "UPDATE org.departments SET head_employee_id = %s WHERE id = %s", (uuid.uuid4(), department)
        )
    head = insert_employee(app_connection)
    app_connection.execute(
        "UPDATE org.departments SET head_employee_id = %s WHERE id = %s", (head, department)
    )


@pytest.mark.parametrize(
    ("kwargs", "violation"),
    [
        ({"joined": "2030-05-01", "exited": "2030-04-30", "status": "exited"}, errors.CheckViolation),
        ({"status": "exited"}, errors.CheckViolation),
        ({"status": "retired"}, errors.CheckViolation),
        ({"email": "not-an-email"}, errors.CheckViolation),
        ({"email": "two@@dev.example"}, errors.CheckViolation),
    ],
)
def test_employee_values_are_checked(
    app_connection: psycopg.Connection, kwargs: dict[str, str], violation: type[Exception]
) -> None:
    with pytest.raises(violation):
        insert_employee(app_connection, **kwargs)


def test_job_ranges_for_one_employee_cannot_overlap(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    employee = insert_employee(app_connection)
    insert_job(app_connection, org, employee, "2030-01-01", effective_to="2030-07-01")
    # The end date is exclusive, so the next row may start on it.
    insert_job(app_connection, org, employee, "2030-07-01")
    with pytest.raises(errors.ExclusionViolation):
        insert_job(app_connection, org, employee, "2030-06-30", effective_to="2030-07-02")
    with pytest.raises(errors.ExclusionViolation):
        insert_job(app_connection, org, employee, "2031-01-01")


def test_other_employees_may_share_dates(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    for _ in range(2):
        insert_job(app_connection, org, insert_employee(app_connection), "2030-01-01")


@pytest.mark.parametrize(
    ("effective_from", "effective_to"),
    [("2030-01-01", "2030-01-01"), ("2030-02-01", "2030-01-01")],
)
def test_job_range_must_not_be_empty(
    app_connection: psycopg.Connection, effective_from: str, effective_to: str
) -> None:
    org = Org(app_connection)
    with pytest.raises(errors.CheckViolation):
        insert_job(
            app_connection, org, insert_employee(app_connection), effective_from, effective_to=effective_to
        )


def test_employee_cannot_manage_themselves(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    employee = insert_employee(app_connection)
    with pytest.raises(errors.CheckViolation):
        insert_job(app_connection, org, employee, "2030-01-01", manager=employee)


def test_job_values_and_references_are_checked(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    with pytest.raises(errors.CheckViolation):
        insert_job(app_connection, org, insert_employee(app_connection), "2030-01-01", employment_type="gig")
    with pytest.raises(errors.ForeignKeyViolation):
        insert_job(app_connection, org, insert_employee(app_connection), "2030-01-01", manager=uuid.uuid4())


def test_referenced_org_units_cannot_be_deleted(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    insert_job(app_connection, org, insert_employee(app_connection), "2030-01-01")
    for table, row_id in (
        ("departments", org.department),
        ("designations", org.designation),
        ("locations", org.location),
    ):
        # ON DELETE RESTRICT reports restrict_violation rather than foreign_key_violation.
        with pytest.raises(errors.RestrictViolation):
            app_connection.execute(f"DELETE FROM org.{table} WHERE id = %s", (row_id,))


def test_closing_a_job_row_is_the_only_allowed_change(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    employee = insert_employee(app_connection)
    job = insert_job(app_connection, org, employee, "2030-01-01")

    app_connection.execute(
        "UPDATE people.employee_jobs SET effective_to = '2030-09-01', updated_at = now() WHERE id = %s",
        (job,),
    )
    other_department = insert_named(app_connection, "departments")
    for assignment, value in (
        ("department_id = %s", other_department),
        ("effective_from = %s", "2029-12-01"),
        ("manager_employee_id = %s", insert_employee(app_connection)),
        ("notes = %s", "Test note"),
    ):
        with pytest.raises(errors.RaiseException, match="only effective_to can change"):
            app_connection.execute(
                f"UPDATE people.employee_jobs SET {assignment} WHERE id = %s",
                (value, job),
            )


def test_job_history_cannot_be_deleted(app_connection: psycopg.Connection) -> None:
    org = Org(app_connection)
    job = insert_job(app_connection, org, insert_employee(app_connection), "2030-01-01")
    with pytest.raises(errors.RaiseException, match="job history is preserved"):
        app_connection.execute("DELETE FROM people.employee_jobs WHERE id = %s", (job,))


def test_job_history_cannot_be_truncated_even_by_the_owner(migrated_postgres: PostgresServer) -> None:
    with migrated_postgres.connect("hrms_migrator") as connection, pytest.raises(errors.RaiseException):
        connection.execute("TRUNCATE people.employee_jobs")


def allowed_values(connection: psycopg.Connection, constraint: str) -> set[str]:
    row = connection.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (constraint,)
    ).fetchone()
    assert row is not None, constraint
    return set(re.findall(r"'([a-z_]+)'::text", row[0]))


@pytest.mark.parametrize(
    ("constraints", "enum"),
    [
        (("ck_locations_status", "ck_departments_status", "ck_designations_status"), OrgUnitStatus),
        (("ck_employees_status",), EmployeeStatus),
        (("ck_employee_jobs_employment_type",), EmploymentType),
        (("ck_employee_jobs_change_reason",), JobChangeReason),
    ],
)
def test_python_enums_mirror_database_checks(
    app_connection: psycopg.Connection, constraints: tuple[str, ...], enum: type[StrEnum]
) -> None:
    for constraint in constraints:
        assert allowed_values(app_connection, constraint) == {member.value for member in enum}
