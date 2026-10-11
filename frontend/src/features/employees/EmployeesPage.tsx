/**
 * Employee records within the viewer's scope (authorization-model.md §3.2): HR sees everyone
 * (or their department or location), managers their team. Filters live in the URL so a view
 * can be bookmarked; the receiver's own permissions still apply.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { UserPlus, Users } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { ButtonLink } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import { formatDate } from "../../lib/format";
import { options, useDepartments, useDesignations, useLocations } from "../organization/queries";
import {
  PEOPLE_KEY,
  STATUS_LABELS,
  STATUS_TONES,
  type EmployeeStatus,
  type EmployeeSummary,
} from "./queries";

const ANY = "any";
// By dates on the day (product-requirements EMP-7), the same definitions as the dashboard.
const EMPLOYMENT_FILTERS = [
  { id: "current", label: "Employed today" },
  { id: "future", label: "Joining in the future" },
  { id: "former", label: "Former employees" },
  { id: "all", label: "Everyone" },
];
type Employment = "current" | "future" | "former";
// The status HR recorded, which can lag the dates (for example, not yet marked active).
const STATUS_FILTERS = [
  { id: ANY, label: "Any status" },
  { id: "active", label: "Active" },
  { id: "on_notice", label: "On notice" },
  { id: "pre_joining", label: "Joining soon" },
  { id: "exited", label: "Left" },
];

const SORTS = [
  { id: "name", label: "Name" },
  { id: "code", label: "Employee code" },
  { id: "joined_newest", label: "Joined, newest first" },
  { id: "joined_oldest", label: "Joined, oldest first" },
] as const;
type Sort = (typeof SORTS)[number]["id"];

function sortOf(value: string | null): Sort {
  return SORTS.find((item) => item.id === value)?.id ?? "name";
}

function statusQuery(value: string): EmployeeStatus[] {
  return value === ANY ? [] : [value as EmployeeStatus];
}

export function EmployeesPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const status = params.get("status") ?? ANY;
  const employment = params.get("employment") ?? "current";
  const department = params.get("department") ?? ANY;
  const designation = params.get("designation") ?? ANY;
  const location = params.get("location") ?? ANY;
  const sort = sortOf(params.get("sort"));
  const [text, setText] = useState(q);
  const canReadOrg = hasPermission(me.data, "org.read");
  const departments = useDepartments(false, canReadOrg);
  const designations = useDesignations(false, canReadOrg);
  const locations = useLocations(false, canReadOrg);

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (
      value === "" ||
      value === ANY ||
      (key === "sort" && value === "name") ||
      (key === "employment" && value === "current")
    )
      next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const list = useInfiniteQuery({
    queryKey: [...PEOPLE_KEY, "list", q, employment, status, department, designation, location, sort],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/employees", {
          params: {
            query: {
              ...(q ? { q } : {}),
              status: statusQuery(status),
              ...(employment !== "all" ? { employment: employment as Employment } : {}),
              ...(department !== ANY ? { department_id: department } : {}),
              ...(designation !== ANY ? { designation_id: designation } : {}),
              ...(location !== ANY ? { location_id: location } : {}),
              sort,
              limit: 50,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered =
    Boolean(q) ||
    employment !== "current" ||
    status !== ANY ||
    department !== ANY ||
    designation !== ANY ||
    location !== ANY;
  const canCreate = hasPermission(me.data, "employee.create");

  const columns: Column<EmployeeSummary>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (row) => (
        <Link to={`/employees/${row.id}`} className="flex items-center gap-3 hover:underline">
          <Avatar name={row.name} size="sm" />
          <span className="min-w-0">
            <span className="block truncate">{row.name}</span>
            <span className="block text-xs font-normal text-neutral-600">{row.employee_code}</span>
          </span>
        </Link>
      ),
    },
    { key: "designation", header: "Designation", cell: (row) => row.job?.designation.name ?? "Not placed" },
    { key: "department", header: "Department", cell: (row) => row.job?.department.name ?? "Not placed" },
    { key: "location", header: "Location", cell: (row) => row.job?.location.name ?? "Not placed" },
    { key: "manager", header: "Manager", cell: (row) => row.job?.manager?.name ?? "None" },
    { key: "joined", header: "Joined", cell: (row) => formatDate(row.date_of_joining, { weekday: false }) },
    {
      key: "status",
      header: "Status",
      cell: (row) => <Badge tone={STATUS_TONES[row.status]}>{STATUS_LABELS[row.status]}</Badge>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Employees"
        description="Employee records you can read: your team, or everyone your HR role covers."
        action={
          canCreate ? (
            <ButtonLink to="/employees/new">
              <UserPlus aria-hidden className="size-4" />
              Add employee
            </ButtonLink>
          ) : undefined
        }
      />
      <div className="mb-4 flex flex-col gap-3">
        <SearchField
          label="Search employees"
          placeholder="Search by name, code or email"
          value={text}
          onChange={(value) => {
            setText(value);
            if (value === "") update("q", "");
          }}
          onSubmit={(value) => {
            update("q", value.trim());
          }}
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 xl:items-end">
          <Select
            label="Employment"
            name="employment"
            options={EMPLOYMENT_FILTERS}
            value={employment}
            onChange={(value) => {
              update("employment", value);
            }}
            description="By the joining date and last working day."
          />
          <Select
            label="Status"
            name="status"
            options={STATUS_FILTERS}
            value={status}
            onChange={(value) => {
              update("status", value);
            }}
          />
          {canReadOrg ? (
            <>
              <Select
                label="Department"
                name="department"
                options={[{ id: ANY, label: "All departments" }, ...options(departments.data?.items)]}
                value={department}
                onChange={(value) => {
                  update("department", value);
                }}
              />
              <Select
                label="Designation"
                name="designation"
                options={[{ id: ANY, label: "All designations" }, ...options(designations.data?.items)]}
                value={designation}
                onChange={(value) => {
                  update("designation", value);
                }}
              />
              <Select
                label="Location"
                name="location"
                options={[{ id: ANY, label: "All locations" }, ...options(locations.data?.items)]}
                value={location}
                onChange={(value) => {
                  update("location", value);
                }}
              />
            </>
          ) : null}
          <Select
            label="Sort by"
            name="sort"
            options={SORTS.map((item) => ({ id: item.id, label: item.label }))}
            value={sort}
            onChange={(value) => {
              update("sort", value);
            }}
          />
        </div>
      </div>
      {list.isPending ? <Skeleton label="Loading employees" lines={6} /> : null}
      {list.isError ? (
        <ProblemMessage
          error={list.error}
          fallback="We couldn't load the employees."
          onRetry={() => void list.refetch()}
        />
      ) : null}
      {list.isSuccess && rows.length === 0 ? (
        filtered ? (
          <EmptyState icon={Users} title="No employees match these filters">
            Change the search or filters to see more people.
          </EmptyState>
        ) : (
          <EmptyState
            icon={Users}
            title="No employee records yet"
            action={
              canCreate ? (
                <ButtonLink to="/employees/new" size="sm">
                  Add the first employee
                </ButtonLink>
              ) : undefined
            }
          >
            {canCreate
              ? "Add employees once departments, designations and locations exist in Organization."
              : "Employee records you can read appear here."}
          </EmptyState>
        )
      ) : null}
      {rows.length > 0 ? (
        <>
          <DataTable label="Employees" columns={columns} rows={rows} rowKey={(row) => row.id} />
          <LoadMore
            shown={rows.length}
            hasMore={list.hasNextPage}
            isLoading={list.isFetchingNextPage}
            onLoad={() => void list.fetchNextPage()}
          />
        </>
      ) : null}
    </div>
  );
}
