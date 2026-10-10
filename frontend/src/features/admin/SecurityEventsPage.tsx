/**
 * Security events (`security.event.read`, docs/product-requirements.md SEC-1): sign-ins,
 * lockouts, MFA changes, elevations and break-glass. Every page read is itself recorded in the
 * audit log, so the list loads only when asked for.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { useState } from "react";

import { Badge, type BadgeTone } from "../../design-system/Badge";
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

type SecurityRecord = components["schemas"]["SecurityRecord"];

const SEVERITY_TONES: Record<string, BadgeTone> = { info: "neutral", warning: "warning", high: "danger" };

function details(record: SecurityRecord): string {
  if (!record.details) return "None";
  return Object.entries(record.details)
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`)
    .join(", ");
}

export function SecurityEventsPage() {
  const [filters, setFilters] = useState<RecordFilterValues>({ from: "", to: "", text: "" });
  const events = useInfiniteQuery({
    queryKey: ["audit", "security-events", filters],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/security-events", {
          params: {
            query: {
              ...rangeQuery(filters),
              ...(filters.text ? { event_type: filters.text } : {}),
              limit: 50,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = events.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<SecurityRecord>[] = [
    { key: "when", header: "When", primary: true, cell: (row) => formatDateTime(row.occurred_at) },
    {
      key: "event",
      header: "Event",
      cell: (row) => <code className="font-mono text-xs">{row.event_type}</code>,
    },
    {
      key: "severity",
      header: "Severity",
      cell: (row) => <Badge tone={SEVERITY_TONES[row.severity] ?? "neutral"}>{row.severity}</Badge>,
    },
    {
      key: "user",
      header: "Account",
      cell: (row) =>
        row.user_id ? <code className="font-mono text-xs">{row.user_id.slice(-12)}</code> : "None",
    },
    { key: "ip", header: "Address", cell: (row) => row.ip ?? "Unknown" },
    { key: "details", header: "Details", cell: (row) => <span className="text-xs">{details(row)}</span> },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Security events"
        description="Sign-ins, lockouts, MFA changes, elevations and break-glass, newest first. Your reading of this list is recorded in the audit log."
      />
      <RecordFilters
        textLabel="Event type (optional)"
        textDescription="An exact type, such as login.failed."
        onApply={setFilters}
      />
      {events.isPending ? <Skeleton label="Loading security events" lines={6} /> : null}
      {events.isError ? (
        <ProblemMessage
          error={events.error}
          fallback="We couldn't load the security events."
          onRetry={() => void events.refetch()}
        />
      ) : null}
      {events.isSuccess && rows.length === 0 ? (
        <EmptyState icon={Activity} title="No security events in this range" />
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable label="Security events" columns={columns} rows={rows} rowKey={(row) => row.id} />
          <LoadMore
            shown={rows.length}
            hasMore={events.hasNextPage}
            isLoading={events.isFetchingNextPage}
            onLoad={() => void events.fetchNextPage()}
          />
        </div>
      ) : null}
    </div>
  );
}
