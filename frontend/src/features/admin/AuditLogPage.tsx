/**
 * The audit log (`audit.read`): who did what, under which permission and, for elevated
 * sessions, which access request. Values of personal or sensitive fields are never in it; the
 * row says only that they changed. Reading the log is itself recorded.
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

function changeText(value: unknown): string {
  if (typeof value !== "object" || value === null) return String(value);
  const change = value as { old?: unknown; new?: unknown; redacted?: boolean };
  if (change.redacted) return "changed (value not recorded)";
  const show = (item: unknown) => (item === null || item === undefined ? "empty" : JSON.stringify(item));
  return `${show(change.old)} to ${show(change.new)}`;
}

function Changes({ record }: { record: AuditRecord }) {
  const entries = Object.entries(record.changes ?? {});
  if (entries.length === 0) return <span className="text-neutral-500">None</span>;
  return (
    <details>
      <summary className="cursor-pointer text-accent-700">{entries.length} fields</summary>
      <ul className="mt-1 flex flex-col gap-0.5 text-xs">
        {entries.map(([field, value]) => (
          <li key={field}>
            <span className="font-semibold">{field.replace(/_/g, " ")}</span>: {changeText(value)}
          </li>
        ))}
      </ul>
    </details>
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
          {row.outcome !== "success" ? <Badge tone="danger">{row.outcome}</Badge> : null}
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
    { key: "changes", header: "Changes", cell: (row) => <Changes record={row} /> },
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
          <DataTable label="Audit log" columns={columns} rows={rows} rowKey={(row) => row.id} />
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
