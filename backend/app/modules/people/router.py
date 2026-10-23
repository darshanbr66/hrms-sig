"""HTTP routes for the directory, employee records and self-service profile
(docs/api-architecture.md §8, People and Me)."""

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from app.modules.people import schemas as s
from app.modules.people.models import EmergencyContact, EmployeeJob, EmployeePersonal, EmployeeStatusChange
from app.modules.people.repository import EmployeeFilters, FormerFilters, name_of
from app.modules.people.service import (
    EmployeeListing,
    EmployeeRecord,
    JobChangeRequest,
    Names,
    NewEmployee,
    PeopleService,
)
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version
from app.platform.pagination import decode_key_cursor, encode_key_cursor

router = APIRouter(prefix="/api/v1", tags=["people"])

MAX_LIMIT: Final = 100
SearchText = Annotated[str | None, Query(min_length=1, max_length=100)]
Cursor = Annotated[str | None, Query(max_length=400)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


def service(request: Request) -> PeopleService:
    value: PeopleService = request.app.state.people_service
    return value


Service = Annotated[PeopleService, Depends(service)]


def _sent(body: BaseModel) -> dict[str, Any]:
    return {name: getattr(body, name) for name in body.model_fields_set}


def _unit(unit_id: uuid.UUID, names: dict[uuid.UUID, str]) -> s.UnitRef:
    return s.UnitRef(id=unit_id, name=names.get(unit_id, ""))


def _manager(job: EmployeeJob, names: Names) -> s.PersonRef | None:
    if job.manager_employee_id is None:
        return None
    return s.PersonRef(id=job.manager_employee_id, name=names.people.get(job.manager_employee_id, ""))


def _current_job(job: EmployeeJob | None, names: Names) -> s.CurrentJob | None:
    if job is None:
        return None
    return s.CurrentJob(
        department=_unit(job.department_id, names.units.departments),
        designation=_unit(job.designation_id, names.units.designations),
        location=_unit(job.location_id, names.units.locations),
        manager=_manager(job, names),
        employment_type=job.employment_type,
        effective_from=job.effective_from,
    )


def _summary(record: EmployeeRecord) -> s.EmployeeSummary:
    employee = record.employee
    return s.EmployeeSummary(
        id=employee.id,
        employee_code=employee.employee_code,
        name=name_of(employee),
        preferred_name=employee.preferred_name,
        work_email=employee.work_email,
        status=employee.status,
        date_of_joining=employee.date_of_joining,
        job=_current_job(record.job, record.names),
    )


def _view(record: EmployeeRecord, response: Response) -> s.EmployeeView:
    employee = record.employee
    response.headers["ETag"] = etag(employee.version)
    return s.EmployeeView(
        id=employee.id,
        employee_code=employee.employee_code,
        name=name_of(employee),
        legal_first_name=employee.legal_first_name,
        legal_last_name=employee.legal_last_name,
        preferred_name=employee.preferred_name,
        work_email=employee.work_email,
        work_phone=employee.work_phone,
        status=employee.status,
        date_of_joining=employee.date_of_joining,
        date_of_exit=employee.date_of_exit,
        job=_current_job(record.job, record.names),
        version=employee.version,
        created_at=employee.created_at,
        updated_at=employee.updated_at,
    )


def _directory_entry(record: EmployeeRecord) -> s.DirectoryEntry:
    employee, job, names = record.employee, record.job, record.names
    return s.DirectoryEntry(
        id=employee.id,
        name=f"{employee.legal_first_name} {employee.legal_last_name or ''}".strip(),
        preferred_name=employee.preferred_name,
        work_email=employee.work_email,
        department=_unit(job.department_id, names.units.departments) if job else None,
        designation=_unit(job.designation_id, names.units.designations) if job else None,
        location=_unit(job.location_id, names.units.locations) if job else None,
    )


def _jobs(jobs: list[EmployeeJob], names: Names, *, include_notes: bool) -> s.JobList:
    return s.JobList(
        items=[
            s.JobView(
                id=job.id,
                effective_from=job.effective_from,
                effective_to=job.effective_to,
                department=_unit(job.department_id, names.units.departments),
                designation=_unit(job.designation_id, names.units.designations),
                location=_unit(job.location_id, names.units.locations),
                manager=_manager(job, names),
                employment_type=job.employment_type,
                change_reason=job.change_reason,
                notes=job.notes if include_notes else None,
            )
            for job in jobs
        ]
    )


def _status_change(record: EmployeeStatusChange) -> s.StatusChangeView:
    return s.StatusChangeView(
        id=record.id,
        from_status=record.from_status,
        to_status=record.to_status,
        effective_date=record.effective_date,
        reason=record.reason,
        created_at=record.created_at,
    )


def _personal(record: EmployeePersonal | None, response: Response) -> s.PersonalView:
    version = record.version if record else 0
    response.headers["ETag"] = etag(version)
    if record is None:
        return s.PersonalView(
            date_of_birth=None,
            personal_email=None,
            personal_phone=None,
            current_address=None,
            permanent_address=None,
            version=0,
        )
    return s.PersonalView(
        date_of_birth=record.date_of_birth,
        personal_email=record.personal_email,
        personal_phone=record.personal_phone,
        current_address=s.Address.model_validate(record.current_address) if record.current_address else None,
        permanent_address=s.Address.model_validate(record.permanent_address)
        if record.permanent_address
        else None,
        version=version,
    )


def _personal_values(body: s.PersonalUpdate) -> dict[str, Any]:
    values = _sent(body)
    for name in ("current_address", "permanent_address"):
        if values.get(name) is not None:
            values[name] = values[name].model_dump(exclude_none=True)
    return values


def _page_after(cursor: str | None) -> tuple[str, uuid.UUID] | None:
    return decode_key_cursor(cursor)


def _next(listing: EmployeeListing) -> str | None:
    return encode_key_cursor(listing.next_after)


# --- directory and employee lists ---------------------------------------------------------------


@router.get("/directory", response_model=s.DirectoryPage)
async def read_directory(
    *,
    people: Service,
    _actor: Annotated[Actor, requires("employee.directory.read")],
    q: SearchText = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.DirectoryPage:
    filters = EmployeeFilters(text=q, department_id=department_id, location_id=location_id)
    listing = await people.directory(filters, _page_after(cursor), limit)
    return s.DirectoryPage(items=[_directory_entry(r) for r in listing.records], next_cursor=_next(listing))


@router.get("/employees", response_model=s.EmployeePage)
async def list_employees(
    *,
    people: Service,
    actor: Annotated[Actor, requires("employee.profile.read")],
    q: SearchText = None,
    employee_status: Annotated[list[s.StatusValue] | None, Query(alias="status", max_length=4)] = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employment: s.Employment | None = None,
    sort: s.EmployeeSort = "name",
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.EmployeePage:
    filters = EmployeeFilters(
        text=q,
        statuses=tuple(employee_status or ()),
        department_id=department_id,
        designation_id=designation_id,
        location_id=location_id,
        manager_id=manager_id,
        employment=employment,
        sort=sort,
    )
    listing = await people.employees(actor, filters, _page_after(cursor), limit)
    return s.EmployeePage(items=[_summary(r) for r in listing.records], next_cursor=_next(listing))


@router.get("/former-employees", response_model=s.FormerEmployeePage)
async def list_former_employees(
    *,
    people: Service,
    actor: Annotated[Actor, requires("employee.profile.read")],
    q: SearchText = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    account: s.AccountState | None = None,
    sort: s.FormerSort = "left_newest",
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.FormerEmployeePage:
    """People who have been offboarded, at the reader's scope, with their last job and their
    account's state (for readers holding `user.read.all`; the `account` filter needs it too).
    Erased records are not listed. Read-only."""
    filters = FormerFilters(
        text=q, department_id=department_id, designation_id=designation_id, location_id=location_id, sort=sort
    )
    former = await people.former_employees(
        actor, filters, account=account, after=_page_after(cursor), limit=limit
    )
    accounts = former.accounts or {}
    items = []
    for record in former.listing.records:
        employee = record.employee
        linked = accounts.get(employee.id)
        if employee.date_of_exit is None:
            raise RuntimeError("an exited employee record has no last working day")
        items.append(
            s.FormerEmployee(
                id=employee.id,
                employee_code=employee.employee_code,
                name=name_of(employee),
                date_of_joining=employee.date_of_joining,
                date_of_exit=employee.date_of_exit,
                job=_current_job(record.job, record.names),
                account=s.FormerAccount(id=linked.user_id, email=linked.email, status=linked.status.value)
                if linked is not None
                else None,
            )
        )
    return s.FormerEmployeePage(
        items=items, next_cursor=_next(former.listing), accounts_visible=former.accounts is not None
    )


@router.get("/employees/statistics", response_model=s.EmployeeStatistics)
async def employee_statistics(
    people: Service, actor: Annotated[Actor, requires("employee.profile.read")]
) -> s.EmployeeStatistics:
    stats = await people.statistics(actor)
    return s.EmployeeStatistics(
        employed=stats.employed,
        active=stats.active,
        on_notice=stats.on_notice,
        pre_joining=stats.pre_joining,
        future_joiners=stats.future_joiners,
        former=stats.former,
        joined_last_30_days=stats.joined_recently,
        starting_soon=[
            s.StartingSoon(employee=s.PersonRef(id=i, name=n), date_of_joining=d)
            for i, n, d in stats.starting_soon
        ],
        work_anniversaries=[
            s.Anniversary(employee=s.PersonRef(id=i, name=n), date=d, years=y)
            for i, n, d, y in stats.anniversaries
        ],
        recent_joiners=[
            s.RecentJoiner(employee=s.PersonRef(id=i, name=n), date_of_joining=d)
            for i, n, d in stats.recent_joiners
        ],
        by_department=[s.Headcount(id=i, name=n, employed=c) for i, n, c in stats.by_department],
        by_designation=[s.Headcount(id=i, name=n, employed=c) for i, n, c in stats.by_designation],
        by_location=[s.Headcount(id=i, name=n, employed=c) for i, n, c in stats.by_location],
    )


@router.post("/employees", response_model=s.EmployeeView, status_code=status.HTTP_201_CREATED)
async def create_employee(
    body: s.EmployeeCreate,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.create")],
) -> s.EmployeeView:
    values = body.model_dump(exclude={"job"})
    record = await people.create(
        actor,
        RequestContext.from_request(request),
        NewEmployee(
            values=values,
            department_id=body.job.department_id,
            designation_id=body.job.designation_id,
            location_id=body.job.location_id,
            manager_employee_id=body.job.manager_employee_id,
            employment_type=body.job.employment_type,
        ),
    )
    return _view(record, response)


# --- one employee -------------------------------------------------------------------------------


@router.get("/employees/{employee_id}", response_model=s.EmployeeView)
async def get_employee(
    employee_id: uuid.UUID,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.profile.read")],
) -> s.EmployeeView:
    return _view(await people.get(actor, employee_id), response)


@router.patch("/employees/{employee_id}", response_model=s.EmployeeView)
async def update_employee(
    *,
    employee_id: uuid.UUID,
    body: s.EmployeeUpdate,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.profile.update.all")],
    if_match: IfMatch = None,
) -> s.EmployeeView:
    record = await people.update(
        actor, RequestContext.from_request(request), employee_id, expected_version(if_match), _sent(body)
    )
    return _view(record, response)


@router.get("/employees/{employee_id}/jobs", response_model=s.JobList)
async def employee_jobs(
    employee_id: uuid.UUID, people: Service, actor: Annotated[Actor, requires("employee.profile.read")]
) -> s.JobList:
    history = await people.jobs(actor, employee_id)
    return _jobs(history.jobs, history.names, include_notes=history.include_notes)


@router.post("/employees/{employee_id}/jobs", response_model=s.JobList, status_code=status.HTTP_201_CREATED)
async def change_job(
    employee_id: uuid.UUID,
    body: s.JobChange,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.lifecycle.manage")],
) -> s.JobList:
    context = RequestContext.from_request(request)
    await people.change_job(
        actor,
        context,
        employee_id,
        JobChangeRequest(
            effective_from=body.effective_from,
            department_id=body.department_id,
            designation_id=body.designation_id,
            location_id=body.location_id,
            manager_employee_id=body.manager_employee_id,
            employment_type=body.employment_type,
            change_reason=body.change_reason,
            notes=body.notes,
        ),
    )
    history = await people.jobs(actor, employee_id)
    return _jobs(history.jobs, history.names, include_notes=history.include_notes)


@router.get("/employees/{employee_id}/status-changes", response_model=s.StatusHistory)
async def employee_status_history(
    employee_id: uuid.UUID, people: Service, actor: Annotated[Actor, requires("employee.profile.read")]
) -> s.StatusHistory:
    return s.StatusHistory(items=[_status_change(r) for r in await people.status_history(actor, employee_id)])


@router.post(
    "/employees/{employee_id}/status-changes",
    response_model=s.StatusChangeView,
    status_code=status.HTTP_201_CREATED,
)
async def change_employee_status(
    employee_id: uuid.UUID,
    body: s.StatusChangeCreate,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.lifecycle.manage")],
) -> s.StatusChangeView:
    """Records a status change. To record someone leaving, use `POST /employees/{id}/exit`,
    which also disables their account and revokes their sessions in the same step."""
    record = await people.change_status(
        actor,
        RequestContext.from_request(request),
        employee_id,
        to_status=body.to_status,
        effective_date=body.effective_date,
        reason=body.reason,
    )
    return _status_change(record)


@router.post("/employees/{employee_id}/exit", response_model=s.ExitView, status_code=status.HTTP_201_CREATED)
async def exit_employee(
    employee_id: uuid.UUID,
    body: s.ExitCreate,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.lifecycle.manage")],
) -> s.ExitView:
    """The employee leaves: the record is marked exited from the last working day and their
    account is disabled with every session revoked, in one step (needs `user.disable` and a
    recent step-up when an account is linked)."""
    result = await people.exit_employee(
        actor,
        RequestContext.from_request(request),
        employee_id,
        last_working_day=body.last_working_day,
        reason=body.reason,
    )
    return s.ExitView(
        status_change=_status_change(result.status_change),
        account=result.account,
        sessions_revoked=result.sessions_revoked,
    )


def _contacts(rows: list[EmergencyContact]) -> s.EmergencyContactList:
    return s.EmergencyContactList(
        items=[
            s.EmergencyContactView(
                priority=r.priority, name=r.name, relationship=r.relationship, phone=r.phone
            )
            for r in rows
        ]
    )


@router.get("/employees/{employee_id}/emergency-contacts", response_model=s.EmergencyContactList)
async def employee_emergency_contacts(
    employee_id: uuid.UUID,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.read")],
) -> s.EmergencyContactList:
    """Personal tier: HR (`employee.personal.read.all`), audited. A manager reaches their team's
    contacts only if Sigvitas grants `employee.emergency_contact.read.team`."""
    return _contacts(
        await people.emergency_contacts(actor, RequestContext.from_request(request), employee_id)
    )


@router.put("/employees/{employee_id}/emergency-contacts", response_model=s.EmergencyContactList)
async def replace_employee_emergency_contacts(
    employee_id: uuid.UUID,
    body: s.EmergencyContactsUpdate,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.update.all")],
) -> s.EmergencyContactList:
    rows = await people.replace_emergency_contacts(
        actor, RequestContext.from_request(request), employee_id, [c.model_dump() for c in body.contacts]
    )
    return _contacts(rows)


@router.get("/me/emergency-contacts", response_model=s.EmergencyContactList)
async def my_emergency_contacts(
    request: Request, people: Service, actor: Annotated[Actor, requires("employee.personal.read.self")]
) -> s.EmergencyContactList:
    return _contacts(await people.emergency_contacts(actor, RequestContext.from_request(request), None))


@router.put("/me/emergency-contacts", response_model=s.EmergencyContactList)
async def replace_my_emergency_contacts(
    body: s.EmergencyContactsUpdate,
    request: Request,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.update.self")],
) -> s.EmergencyContactList:
    rows = await people.replace_emergency_contacts(
        actor, RequestContext.from_request(request), None, [c.model_dump() for c in body.contacts]
    )
    return _contacts(rows)


@router.get("/employees/{employee_id}/personal", response_model=s.PersonalView)
async def employee_personal(
    employee_id: uuid.UUID,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.read")],
) -> s.PersonalView:
    return _personal(
        await people.personal(actor, RequestContext.from_request(request), employee_id), response
    )


@router.patch("/employees/{employee_id}/personal", response_model=s.PersonalView)
async def update_employee_personal(
    *,
    employee_id: uuid.UUID,
    body: s.PersonalUpdate,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.update.all")],
    if_match: IfMatch = None,
) -> s.PersonalView:
    record = await people.update_personal(
        actor,
        RequestContext.from_request(request),
        employee_id,
        expected_version(if_match),
        _personal_values(body),
    )
    return _personal(record, response)


# --- self-service -------------------------------------------------------------------------------


@router.get("/me/profile", response_model=s.EmployeeView)
async def my_profile(
    response: Response, people: Service, actor: Annotated[Actor, requires("employee.profile.read.self")]
) -> s.EmployeeView:
    return _view(await people.own(actor), response)


@router.patch("/me/profile", response_model=s.EmployeeView)
async def update_my_profile(
    *,
    body: s.SelfProfileUpdate,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.profile.update.self")],
    if_match: IfMatch = None,
) -> s.EmployeeView:
    record = await people.update_own(
        actor, RequestContext.from_request(request), expected_version(if_match), _sent(body)
    )
    return _view(record, response)


@router.get("/me/jobs", response_model=s.JobList)
async def my_jobs(
    people: Service, actor: Annotated[Actor, requires("employee.profile.read.self")]
) -> s.JobList:
    if actor.employee_id is None:
        return s.JobList(items=[])
    history = await people.jobs(actor, actor.employee_id)
    return _jobs(history.jobs, history.names, include_notes=history.include_notes)


@router.get("/me/personal", response_model=s.PersonalView)
async def my_personal(
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.read.self")],
) -> s.PersonalView:
    return _personal(await people.personal(actor, RequestContext.from_request(request), None), response)


@router.patch("/me/personal", response_model=s.PersonalView)
async def update_my_personal(
    *,
    body: s.PersonalUpdate,
    request: Request,
    response: Response,
    people: Service,
    actor: Annotated[Actor, requires("employee.personal.update.self")],
    if_match: IfMatch = None,
) -> s.PersonalView:
    record = await people.update_personal(
        actor, RequestContext.from_request(request), None, expected_version(if_match), _personal_values(body)
    )
    return _personal(record, response)
