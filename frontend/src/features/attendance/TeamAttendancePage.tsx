/**
 * Where everyone in the viewer's scope stands today (managers: their team; HR: everyone their
 * role covers). Counts cover the whole scope; the list is searchable and paged.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { UsersRound } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { canSee } from "../../app/navigation";
import { useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { Stat, StatGrid } from "../../design-system/Stat";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDuration, formatTime } from "../../lib/format";
import { ATTENDANCE_KEY, STATE_LABELS } from "./queries";

type Member = components["schemas"]["TeamMemberToday"];

export function TeamAttendancePage() {
  const me = useMe();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const canOpenRecords = canSee(me.data, {
    anyOf: ["employee.profile.read.team", "employee.profile.read.all"],
  });
  const team = useInfiniteQuery({
    queryKey: [...ATTENDANCE_KEY, "team", q],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/attendance/today", {
          params: { query: { ...(q ? { q } : {}), limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 120_000,
  });
  const summary = team.data?.pages[0]?.summary;
  const rows = team.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<Member>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (row) => (
        <span className="flex items-center gap-3">
          <Avatar name={row.employee.name} size="sm" />
          <span className="min-w-0">
            {canOpenRecords ? (
              <Link className="block truncate hover:underline" to={`/employees/${row.employee.id}`}>
                {row.employee.name}
              </Link>
            ) : (
              <span className="block truncate">{row.employee.name}</span>
            )}
            <span className="block text-xs font-normal text-neutral-600">
              {[row.employee.designation, row.employee.department].filter(Boolean).join(" · ")}
            </span>
          </span>
        </span>
      ),
    },
    {
      key: "state",
      header: "Now",
      cell: (row) => (
        <Badge
          tone={row.state === "clocked_in" ? "success" : row.state === "on_break" ? "warning" : "neutral"}
        >
          {STATE_LABELS[row.state]}
        </Badge>
      ),
    },
    { key: "first", header: "First in", cell: (row) => (row.first_in ? formatTime(row.first_in) : "None") },
    {
      key: "session",
      header: "Clocked in",
      align: "end",
      cell: (row) => formatDuration(row.session_seconds),
    },
    { key: "break", header: "Breaks", align: "end", cell: (row) => formatDuration(row.break_seconds) },
    { key: "location", header: "Location", cell: (row) => row.employee.location ?? "Not placed" },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Team attendance"
        description="Who has clocked in today. Times are in your time zone; each person's day follows their location."
      />
      {summary ? (
        <StatGrid label="Today's attendance">
          <Stat label="People" value={summary.in_scope} hint="Employed today and visible to you" />
          <Stat label="Clocked in today" value={summary.clocked_in_today} />
          <Stat label="Working now" value={summary.working_now} />
          <Stat label="On a break" value={summary.on_break} />
        </StatGrid>
      ) : null}
      <div className="max-w-md">
        <SearchField
          label="Search people"
          placeholder="Search by name or code"
          value={text}
          onChange={(value) => {
            setText(value);
            if (value === "") setQ("");
          }}
          onSubmit={(value) => {
            setQ(value.trim());
          }}
        />
      </div>
      {team.isPending ? <Skeleton label="Loading team attendance" lines={6} /> : null}
      {team.isError ? (
        <ProblemMessage
          error={team.error}
          fallback="We couldn't load team attendance."
          onRetry={() => void team.refetch()}
        />
      ) : null}
      {team.isSuccess && rows.length === 0 ? (
        <EmptyState icon={UsersRound} title={q ? "Nobody matches this search" : "Nobody to show yet"}>
          {q ? "Try another name or code." : "People you can see appear here once they have a current job."}
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable
            label="Team attendance today"
            columns={columns}
            rows={rows}
            rowKey={(row) => row.employee.id}
          />
          <LoadMore
            shown={rows.length}
            hasMore={team.hasNextPage}
            isLoading={team.isFetchingNextPage}
            onLoad={() => void team.fetchNextPage()}
          />
        </div>
      ) : null}
    </div>
  );
}
