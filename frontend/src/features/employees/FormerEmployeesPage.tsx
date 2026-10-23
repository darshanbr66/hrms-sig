/**
 * Former employees: people who have been offboarded, with their last job and, for readers who
 * may see accounts (`user.read.all`), their account's state. The account list on Users leaves
 * them out, so it shows the people who can use the system. Read-only: offboarding is final
 * here, accounts are not enabled from this page, and records whose data was erased are not
 * listed (super admins see those under Data erasure). An account still active after the exit
 * is an administrator account the exit left for a system admin to disable; it is shown as a
 * warning, not hidden.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { UserMinus } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate } from "../../lib/format";
import { options, useDepartments, useDesignations, useLocations } from "../organization/queries";
import { PEOPLE_KEY } from "./queries";

type Former = components["schemas"]["FormerEmployee"];
type AccountState = "none" | "active" | "invited" | "disabled";

const ANY = "any";
const SORTS = [
  { id: "left_newest", label: "Left, most recent first" },
  { id: "name", label: "Name" },
] as const;
type Sort = (typeof SORTS)[number]["id"];
const ACCOUNT_FILTERS: { id: AccountState | typeof ANY; label: string }[] = [
  { id: ANY, label: "Any account state" },
  { id: "disabled", label: "Account disabled" },
  { id: "active", label: "Account still active" },
  { id: "invited", label: "Invite not accepted" },
  { id: "none", label: "No account" },
];

function sortOf(value: string | null): Sort {
  return SORTS.find((item) => item.id === value)?.id ?? "left_newest";
}

function AccountCell({ row, canOpen }: { row: Former; canOpen: boolean }) {
  const account = row.account;
  if (!account) return <span className="text-neutral-600">No account</span>;
  const badge =
    account.status === "active" ? (
      <Badge tone="danger">Still active</Badge>
    ) : account.status === "invited" ? (
      <Badge tone="info">Invite not accepted</Badge>
    ) : (
      <Badge tone="neutral">Disabled</Badge>
    );
  return (
    <span className="flex flex-col gap-1">
      {badge}
      {canOpen ? (
        <Link className="truncate text-xs text-accent-700 underline" to={`/admin/users/${account.id}`}>
          {account.email}
        </Link>
      ) : (
        <span className="truncate text-xs text-neutral-600">{account.email}</span>
      )}
    </span>
  );
}

export function FormerEmployeesPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const department = params.get("department") ?? ANY;
  const designation = params.get("designation") ?? ANY;
  const location = params.get("location") ?? ANY;
  const account = params.get("account") ?? ANY;
  const sort = sortOf(params.get("sort"));
  const [text, setText] = useState(q);
  const canReadOrg = hasPermission(me.data, "org.read");
  const canReadAccounts = hasPermission(me.data, "user.read.all");
  const departments = useDepartments(false, canReadOrg);
  const designations = useDesignations(false, canReadOrg);
  const locations = useLocations(false, canReadOrg);

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value === "" || value === ANY || (key === "sort" && value === "left_newest")) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const list = useInfiniteQuery({
    queryKey: [...PEOPLE_KEY, "former", q, department, designation, location, account, sort],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/former-employees", {
          params: {
            query: {
              ...(q ? { q } : {}),
              ...(department !== ANY ? { department_id: department } : {}),
              ...(designation !== ANY ? { designation_id: designation } : {}),
              ...(location !== ANY ? { location_id: location } : {}),
              ...(account !== ANY && canReadAccounts ? { account: account as AccountState } : {}),
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
  const accountsVisible = list.data?.pages[0]?.accounts_visible ?? false;
  const stillActive = rows.filter((row) => row.account?.status === "active").length;
  const filtered =
    Boolean(q) || department !== ANY || designation !== ANY || location !== ANY || account !== ANY;

  const columns: Column<Former>[] = [
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
    {
      key: "designation",
      header: "Last designation",
      cell: (row) => row.job?.designation.name ?? "Not placed",
    },
    { key: "department", header: "Last department", cell: (row) => row.job?.department.name ?? "Not placed" },
    { key: "location", header: "Last location", cell: (row) => row.job?.location.name ?? "Not placed" },
    { key: "joined", header: "Joined", cell: (row) => formatDate(row.date_of_joining, { weekday: false }) },
    {
      key: "left",
      header: "Last working day",
      cell: (row) => formatDate(row.date_of_exit, { weekday: false }),
    },
    ...(accountsVisible
      ? [
          {
            key: "account",
            header: "Account",
            cell: (row: Former) => <AccountCell row={row} canOpen={canReadAccounts} />,
          },
        ]
      : []),
  ];

  return (
    <div>
      <PageHeader
        title="Former employees"
        description="People who have been offboarded, with their last job. Their records and history are kept. Their accounts are not on the Users page, and they cannot be enabled again."
      />
      {stillActive > 0 ? (
        <Banner tone="warning">
          {stillActive === 1 ? "One account" : `${String(stillActive)} accounts`} below{" "}
          {stillActive === 1 ? "is" : "are"} still active after offboarding. The exit does not disable system
          admin or super admin accounts: a system admin must disable them from the account page.
        </Banner>
      ) : null}
      <div className="mb-4 mt-4 flex flex-col gap-3">
        <SearchField
          label="Search former employees"
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
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 xl:items-end">
          {canReadOrg ? (
            <>
              <Select
                label="Last department"
                name="department"
                options={[{ id: ANY, label: "All departments" }, ...options(departments.data?.items)]}
                value={department}
                onChange={(value) => {
                  update("department", value);
                }}
              />
              <Select
                label="Last designation"
                name="designation"
                options={[{ id: ANY, label: "All designations" }, ...options(designations.data?.items)]}
                value={designation}
                onChange={(value) => {
                  update("designation", value);
                }}
              />
              <Select
                label="Last location"
                name="location"
                options={[{ id: ANY, label: "All locations" }, ...options(locations.data?.items)]}
                value={location}
                onChange={(value) => {
                  update("location", value);
                }}
              />
            </>
          ) : null}
          {canReadAccounts ? (
            <Select
              label="Account"
              name="account"
              options={ACCOUNT_FILTERS}
              value={account}
              onChange={(value) => {
                update("account", value);
              }}
            />
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
      {list.isPending ? <Skeleton label="Loading former employees" lines={6} /> : null}
      {list.isError ? (
        <ProblemMessage
          error={list.error}
          fallback="We couldn't load the former employees."
          onRetry={() => void list.refetch()}
        />
      ) : null}
      {list.isSuccess && rows.length === 0 ? (
        <EmptyState
          icon={UserMinus}
          title={filtered ? "No former employees match these filters" : "No former employees"}
        >
          {filtered
            ? "Change the search or filters to see more people."
            : "People appear here once they have been offboarded."}
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <>
          <DataTable label="Former employees" columns={columns} rows={rows} rowKey={(row) => row.id} />
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
