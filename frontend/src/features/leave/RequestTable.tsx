/** A paged list of leave requests: the person's own, the ones waiting for their decision, or
 * every request in their scope. Lists never show the reason; it is on the request page. */
import { useInfiniteQuery } from "@tanstack/react-query";
import { CalendarCheck } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { Badge } from "../../design-system/Badge";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { LoadMore } from "../../design-system/LoadMore";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import type { operations } from "../../lib/api-schema";
import { formatDate } from "../../lib/format";
import {
  LEAVE_KEY,
  STATUS_LABELS,
  STATUS_TONES,
  formatDays,
  formatSpan,
  type RequestSummary,
} from "./queries";

export type RequestQuery = NonNullable<
  operations["list_requests_api_v1_leave_requests_get"]["parameters"]["query"]
>;

export function RequestTable({
  label,
  own = false,
  query = {},
  showEmployee,
  empty,
}: {
  label: string;
  own?: boolean;
  query?: Omit<RequestQuery, "cursor" | "limit">;
  showEmployee: boolean;
  empty: { title: string; body?: ReactNode };
}) {
  const list = useInfiniteQuery({
    queryKey: [...LEAVE_KEY, "requests", own ? "me" : "scope", query],
    queryFn: ({ pageParam }) =>
      own
        ? unwrap(
            client.GET("/api/v1/me/leave-requests", {
              params: {
                query: {
                  limit: 50,
                  ...(query.status ? { status: query.status } : {}),
                  ...(pageParam ? { cursor: pageParam } : {}),
                },
              },
            }),
          )
        : unwrap(
            client.GET("/api/v1/leave-requests", {
              params: { query: { ...query, limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
            }),
          ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<RequestSummary>[] = [
    {
      key: "dates",
      header: "Dates",
      primary: true,
      cell: (row) => (
        <Link to={`/leave/requests/${row.id}`} className="hover:underline">
          {formatSpan(row)}
        </Link>
      ),
    },
    ...(showEmployee
      ? [{ key: "employee", header: "Employee", cell: (row: RequestSummary) => row.employee.name }]
      : []),
    { key: "type", header: "Leave type", cell: (row) => row.leave_type.name },
    { key: "days", header: "Days", align: "end", cell: (row) => formatDays(row.days_requested) },
    {
      key: "status",
      header: "Status",
      cell: (row) => <Badge tone={STATUS_TONES[row.status]}>{STATUS_LABELS[row.status]}</Badge>,
    },
    {
      key: "asked",
      header: "Asked on",
      cell: (row) => formatDate(row.created_at.slice(0, 10), { weekday: false }),
    },
  ];
  if (list.isPending) return <Skeleton label={`Loading ${label.toLowerCase()}`} lines={4} />;
  if (list.isError) {
    return (
      <ProblemMessage
        error={list.error}
        fallback="We couldn't load the leave requests."
        onRetry={() => void list.refetch()}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState icon={CalendarCheck} title={empty.title}>
        {empty.body}
      </EmptyState>
    );
  }
  return (
    <>
      <DataTable label={label} columns={columns} rows={rows} rowKey={(row) => row.id} />
      <LoadMore
        shown={rows.length}
        hasMore={list.hasNextPage}
        isLoading={list.isFetchingNextPage}
        onLoad={() => void list.fetchNextPage()}
      />
    </>
  );
}
