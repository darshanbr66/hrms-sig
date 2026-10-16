/**
 * Reports and exports (product-requirements RPT-1 to RPT-3). A report shows only what the viewer
 * may already read. An export needs the domain's export permission and a fresh confirmation with
 * the authenticator; it is produced in the background, kept privately for 24 hours, and only the
 * person who asked can download it.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link } from "react-router";

import { canSee } from "../../app/navigation";
import { useStepUp } from "../../app/StepUp";
import { hasPermission, useMe } from "../../app/session";
import { Badge, type BadgeTone } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs, type TabItem } from "../../design-system/Tabs";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, formatDateTime, localIsoDate } from "../../lib/format";
import { fieldMessages } from "../../lib/problem";
import { formatDays } from "../leave/queries";
import { options, useDepartments, useLocations } from "../organization/queries";

type Export = components["schemas"]["ExportView"];
type ExportType = Export["export_type"];

const EXPORTS_KEY = ["reports", "exports"] as const;

const EXPORT_TYPES: { id: ExportType; label: string; permission: string; period: boolean; help: string }[] = [
  {
    id: "attendance_summary",
    label: "Attendance summary",
    permission: "attendance.export",
    period: true,
    help: "Per person: days per status, worked, late and overtime totals. At most 62 days.",
  },
  {
    id: "attendance_days",
    label: "Attendance days",
    permission: "attendance.export",
    period: true,
    help: "One row per person and day, as judged. At most 31 days.",
  },
  {
    id: "leave_requests",
    label: "Leave requests",
    permission: "leave.export",
    period: true,
    help: "Requests overlapping the period, without reasons. At most 366 days.",
  },
  {
    id: "leave_balances",
    label: "Leave balances",
    permission: "leave.export",
    period: false,
    help: "Each person's balance per leave type when the file is produced.",
  },
  {
    id: "employees",
    label: "Employees",
    permission: "employee.export",
    period: false,
    help: "Job details and dates. No personal details.",
  },
  {
    id: "audit_log",
    label: "Audit log",
    permission: "audit.export",
    period: true,
    help: "Everything recorded in the audit log in the period. At most 93 days.",
  },
];

const PATHS: Record<ExportType, string> = {
  attendance_summary: "/api/v1/exports/attendance-summary",
  attendance_days: "/api/v1/exports/attendance-days",
  leave_requests: "/api/v1/exports/leave-requests",
  leave_balances: "/api/v1/exports/leave-balances",
  employees: "/api/v1/exports/employees",
  audit_log: "/api/v1/exports/audit-log",
};

const STATUS: Record<Export["status"], { label: string; tone: BadgeTone }> = {
  queued: { label: "Waiting", tone: "info" },
  running: { label: "Being produced", tone: "info" },
  done: { label: "Ready", tone: "success" },
  failed: { label: "Could not be produced", tone: "danger" },
  expired: { label: "Expired", tone: "neutral" },
};

export function ReportsPage() {
  const me = useMe();
  const tabs: TabItem[] = [];
  if (canSee(me.data, { anyOf: ["leave.read.team", "leave.read.all"] })) {
    tabs.push({ id: "leave", label: "Leave report", content: <LeaveReport /> });
  }
  if (EXPORT_TYPES.some((type) => hasPermission(me.data, type.permission))) {
    tabs.push({ id: "exports", label: "Exports", content: <Exports /> });
  }
  return (
    <div>
      <PageHeader
        title="Reports"
        description="Reports show only what you can already see. The attendance report is on the Team attendance page."
        action={
          canSee(me.data, { anyOf: ["attendance.read.team", "attendance.read.all"] }) ? (
            <Link className="text-sm text-accent-700 underline" to="/team/attendance">
              Attendance report
            </Link>
          ) : undefined
        }
      />
      <Tabs label="Reports" items={tabs} />
    </div>
  );
}

function LeaveReport() {
  const today = localIsoDate();
  const [from, setFrom] = useState(`${today.slice(0, 4)}-01-01`);
  const [to, setTo] = useState(today);
  const report = useInfiniteQuery({
    queryKey: ["reports", "leave", from, to],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/leave/report", {
          params: { query: { from, to, limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: from !== "" && to !== "",
  });
  const rows = report.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="grid max-w-md gap-3 sm:grid-cols-2">
        <TextField label="From" name="from" type="date" value={from} onChange={setFrom} />
        <TextField label="To" name="to" type="date" value={to} onChange={setTo} />
      </div>
      <p className="text-sm text-neutral-600">
        Counted days of approved leave and of requests still waiting, per person and leave type, at most 366
        days. People with no leave in the period are not listed.
      </p>
      {report.isPending ? <Skeleton label="Loading the leave report" lines={5} /> : null}
      {report.isError ? (
        <ProblemMessage
          error={report.error}
          fallback="We couldn't load the leave report."
          onRetry={() => void report.refetch()}
        />
      ) : null}
      {report.isSuccess && rows.length === 0 && !report.hasNextPage ? (
        <EmptyState icon={FileSpreadsheet} title="No leave in this period">
          Approved and waiting leave of the people you can see appears here.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <DataTable
          label="Leave report"
          rows={rows}
          rowKey={(row) => `${row.employee.id}-${row.leave_type.id}`}
          columns={[
            {
              key: "name",
              header: "Employee",
              primary: true,
              cell: (row) => `${row.employee.name} (${row.employee_code})`,
            },
            { key: "department", header: "Department", cell: (row) => row.department ?? "Not placed" },
            { key: "type", header: "Leave type", cell: (row) => row.leave_type.name },
            { key: "taken", header: "Taken", align: "end", cell: (row) => formatDays(row.taken) },
            { key: "pending", header: "Waiting", align: "end", cell: (row) => formatDays(row.pending) },
          ]}
        />
      ) : null}
      {report.isSuccess && (rows.length > 0 || report.hasNextPage) ? (
        <LoadMore
          shown={rows.length}
          hasMore={report.hasNextPage}
          isLoading={report.isFetchingNextPage}
          onLoad={() => void report.fetchNextPage()}
        />
      ) : null}
    </div>
  );
}

function Exports() {
  const me = useMe();
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const departments = useDepartments();
  const locations = useLocations();
  const available = EXPORT_TYPES.filter((type) => hasPermission(me.data, type.permission));
  const [type, setType] = useState<string>(available[0]?.id ?? "");
  const [from, setFrom] = useState(`${localIsoDate().slice(0, 7)}-01`);
  const [to, setTo] = useState(localIsoDate());
  const [department, setDepartment] = useState("all");
  const [location, setLocation] = useState("all");
  const chosen = available.find((item) => item.id === type);
  const mine = useQuery({
    queryKey: EXPORTS_KEY,
    queryFn: () => unwrap(client.GET("/api/v1/me/exports")),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.status === "queued" || item.status === "running")
        ? 5_000
        : false,
  });
  const request = useMutation({
    mutationFn: () => {
      const scope = {
        ...(department !== "all" ? { department_id: department } : {}),
        ...(location !== "all" ? { location_id: location } : {}),
      };
      const body =
        chosen?.id === "audit_log" ? { from, to } : chosen?.period ? { from, to, ...scope } : scope;
      return withStepUp(() =>
        unwrap(
          client.POST(PATHS[type as ExportType] as "/api/v1/exports/attendance-summary", {
            body: body as { from: string; to: string },
          }),
        ),
      );
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: EXPORTS_KEY });
      toast("Export requested. It appears below when it is ready.");
    },
  });
  const download = useMutation({
    mutationFn: async (id: string) => {
      const link = await unwrap(
        client.POST("/api/v1/me/exports/{export_id}/download", { params: { path: { export_id: id } } }),
      );
      window.location.assign(link.url);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: EXPORTS_KEY }),
  });
  const errors = fieldMessages(request.error);
  return (
    <div className="flex flex-col gap-6">
      {mine.data && !mine.data.storage_ready ? (
        <Banner tone="warning">
          File storage is not set up, so exports cannot be produced yet. Ask your system administrator.
        </Banner>
      ) : null}
      <Panel
        title="Ask for an export"
        description="You will be asked to confirm it's you with your authenticator app."
      >
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!request.isPending) request.mutate();
          }}
        >
          {request.isError && Object.keys(errors).length === 0 ? (
            <ProblemMessage error={request.error} fallback="We couldn't ask for the export." />
          ) : null}
          <Select
            label="Export"
            name="export_type"
            options={available.map((item) => ({ id: item.id, label: item.label }))}
            value={type}
            onChange={setType}
            {...(chosen ? { description: chosen.help } : {})}
          />
          {chosen?.period ? (
            <div className="grid max-w-md gap-3 sm:grid-cols-2">
              <TextField
                label="From"
                name="from"
                type="date"
                value={from}
                onChange={setFrom}
                error={errors.from}
              />
              <TextField label="To" name="to" type="date" value={to} onChange={setTo} error={errors.to} />
            </div>
          ) : null}
          {chosen && chosen.id !== "audit_log" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Select
                label="Department"
                name="department_id"
                options={[
                  { id: "all", label: "Every department you can see" },
                  ...options(departments.data?.items),
                ]}
                value={department}
                onChange={setDepartment}
              />
              <Select
                label="Location"
                name="location_id"
                options={[
                  { id: "all", label: "Every location you can see" },
                  ...options(locations.data?.items),
                ]}
                value={location}
                onChange={setLocation}
              />
            </div>
          ) : null}
          <div>
            <Button type="submit" isPending={request.isPending} isDisabled={!chosen}>
              Ask for export
            </Button>
          </div>
        </Form>
      </Panel>
      <section aria-labelledby="my-exports">
        <h2 id="my-exports" className="mb-3 text-[1.125rem] font-semibold text-neutral-900">
          My exports
        </h2>
        {mine.isPending ? <Skeleton label="Loading your exports" lines={3} /> : null}
        {mine.isError ? (
          <ProblemMessage
            error={mine.error}
            fallback="We couldn't load your exports."
            onRetry={() => void mine.refetch()}
          />
        ) : null}
        {mine.isSuccess && mine.data.items.length === 0 ? (
          <EmptyState icon={FileSpreadsheet} title="No exports yet">
            Exports you ask for appear here. Each can be downloaded for 24 hours.
          </EmptyState>
        ) : null}
        {download.isError ? (
          <ProblemMessage error={download.error} fallback="We couldn't prepare the download." />
        ) : null}
        {mine.isSuccess && mine.data.items.length > 0 ? (
          <DataTable
            label="My exports"
            rows={mine.data.items}
            rowKey={(row) => row.id}
            columns={[
              {
                key: "type",
                header: "Export",
                primary: true,
                cell: (row) =>
                  EXPORT_TYPES.find((item) => item.id === row.export_type)?.label ?? row.export_type,
              },
              {
                key: "period",
                header: "Period",
                cell: (row) =>
                  row.start && row.end
                    ? `${formatDate(row.start, { weekday: false })} to ${formatDate(row.end, { weekday: false })}`
                    : "As produced",
              },
              { key: "asked", header: "Asked", cell: (row) => formatDateTime(row.requested_at) },
              {
                key: "status",
                header: "Status",
                cell: (row) => <Badge tone={STATUS[row.status].tone}>{STATUS[row.status].label}</Badge>,
              },
              { key: "rows", header: "Rows", align: "end", cell: (row) => row.row_count ?? "" },
              {
                key: "download",
                header: "File",
                cell: (row) =>
                  row.status === "done" ? (
                    <Button
                      size="sm"
                      variant="quiet"
                      onPress={() => {
                        download.mutate(row.id);
                      }}
                      isPending={download.isPending && download.variables === row.id}
                    >
                      <Download aria-hidden className="size-4" />
                      Download
                      {row.expires_at ? (
                        <span className="sr-only"> until {formatDateTime(row.expires_at)}</span>
                      ) : null}
                    </Button>
                  ) : null,
              },
            ]}
          />
        ) : null}
      </section>
    </div>
  );
}
