/**
 * The audit log (`audit.read`): who did what, under which permission and, for elevated
 * sessions, which access request. Values of personal or sensitive fields are never in it; the
 * row says only that they changed. Reading the log is itself recorded.
 *
 * Each row shows the action, outcome, actor, permission and target; it opens to the field
 * changes and the full request context (request, session, address, reason). Refused and
 * failed actions carry a danger badge in words, not colour alone.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { FileClock } from "lucide-react";
import { useState } from "react";

import { Badge } from "../../design-system/Badge";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDateTime } from "../../lib/format";
import { RecordFilters, rangeQuery, type RecordFilterValues } from "./RecordFilters";

type AuditRecord = components["schemas"]["AuditRecord"];

const OUTCOME_LABELS: Record<string, string> = { denied: "Refused", failed: "Failed" };

function changeText(value: unknown): string {
  if (typeof value !== "object" || value === null) return String(value);
  const change = value as { old?: unknown; new?: unknown; redacted?: boolean };
  if (change.redacted) return "changed (value not recorded)";
  const show = (item: unknown) => (item === null || item === undefined ? "empty" : JSON.stringify(item));
  return `${show(change.old)} to ${show(change.new)}`;
}

function RecordDetails({ record }: { record: AuditRecord }) {
  const entries = Object.entries(record.changes ?? {});
  const context = [
    { label: "Occurred", value: formatDateTime(record.occurred_at) },
    { label: "Recorded", value: formatDateTime(record.recorded_at) },
    { label: "Actor", value: record.actor_user_id ?? record.actor_type },
    { label: "Target", value: record.target_id ? `${record.target_type ?? ""} ${record.target_id}` : "None" },
    { label: "Employee concerned", value: record.subject_employee_id ?? "None" },
    { label: "Access request", value: record.grant_request_id ?? "None" },
    { label: "Request", value: record.request_id ?? "None" },
    { label: "Session", value: record.session_id ?? "None" },
    { label: "Address", value: record.ip ?? "None" },
    { label: "Browser", value: record.user_agent ?? "None" },
  ];
  return (
    <div className="grid gap-4 pt-1 text-sm lg:grid-cols-2">
      <div>
        <h3 className="mb-1 text-xs font-semibold text-neutral-600">Changes</h3>
        {entries.length === 0 ? (
          <p className="text-neutral-600">No field changes recorded.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {entries.map(([field, value]) => (
              <li key={field} className="break-words">
                <span className="font-semibold text-neutral-900">{field.replace(/_/g, " ")}</span>:{" "}
                <span className="text-neutral-700">{changeText(value)}</span>
              </li>
            ))}
          </ul>
        )}
        {record.reason ? (
          <p className="mt-2 text-neutral-700">
            <span className="font-semibold text-neutral-900">Reason:</span> {record.reason}
          </p>
        ) : null}
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[auto_minmax(0,1fr)]">
        {context.map((item) => (
          <div key={item.label} className="contents">
            <dt className="text-xs font-semibold text-neutral-600 sm:pt-0.5">{item.label}</dt>
            <dd className="break-all font-mono text-xs text-neutral-800">{item.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function AuditLogPage() {
  const [filters, setFilters] = useState<RecordFilterValues>({ from: "", to: "", text: "" });
  const log = useInfiniteQuery({
    queryKey: ["audit", "log", filters],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/audit-log", {
          params: {
            query: {
              ...rangeQuery(filters),
              ...(filters.text ? { action: filters.text } : {}),
              limit: 50,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = log.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<AuditRecord>[] = [
    { key: "when", header: "When", primary: true, cell: (row) => formatDateTime(row.occurred_at) },
    {
      key: "action",
      header: "Action",
      cell: (row) => (
        <span className="flex flex-col gap-1">
          <code className="font-mono text-xs">{row.action}</code>
          {row.outcome !== "success" ? (
            <Badge tone="danger">{OUTCOME_LABELS[row.outcome] ?? row.outcome}</Badge>
          ) : null}
          {row.grant_request_id ? <Badge tone="warning">Elevated session</Badge> : null}
        </span>
      ),
    },
    {
      key: "actor",
      header: "By",
      cell: (row) =>
        row.actor_user_id ? (
          <code className="font-mono text-xs">{row.actor_user_id.slice(-12)}</code>
        ) : (
          row.actor_type
        ),
    },
    {
      key: "permission",
      header: "Permission",
      cell: (row) => <code className="font-mono text-xs">{row.permission_used ?? "None"}</code>,
    },
    {
      key: "target",
      header: "Target",
      cell: (row) =>
        row.target_type ? `${row.target_type.replace(/_/g, " ")} ${row.target_id?.slice(-12) ?? ""}` : "None",
    },
    {
      key: "changes",
      header: "Changes",
      align: "end",
      cell: (row) => Object.keys(row.changes ?? {}).length,
    },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Audit log"
        description="Every recorded change and sensitive read, newest first. Personal and sensitive values are never stored here. Your reading of this log is recorded too."
      />
      <RecordFilters
        textLabel="Action (optional)"
        textDescription="An exact action, such as employee.created."
        onApply={setFilters}
      />
      {log.isPending ? <Skeleton label="Loading the audit log" lines={6} /> : null}
      {log.isError ? (
        <ProblemMessage
          error={log.error}
          fallback="We couldn't load the audit log."
          onRetry={() => void log.refetch()}
        />
      ) : null}
      {log.isSuccess && rows.length === 0 ? (
        <EmptyState icon={FileClock} title="No audit records in this range" />
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable
            label="Audit log"
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            detailsLabel={(row) => `${row.action}, ${formatDateTime(row.occurred_at)}`}
            details={(row) => <RecordDetails record={row} />}
          />
          <LoadMore
            shown={rows.length}
            hasMore={log.hasNextPage}
            isLoading={log.isFetchingNextPage}
            onLoad={() => void log.fetchNextPage()}
          />
        </div>
      ) : null}
    </div>
  );
}
