/**
 * Attendance of everyone in the viewer's scope (managers: themselves and their team; HR:
 * everyone their role covers): where people stand today, the register of one day as judged,
 * and a period report. Counts cover the whole scope; lists are searchable and paged. A session
 * left open from an earlier day (a forgotten clock-out) is flagged and counts nothing past its
 * last event; HR closes it with the verified departure time (`ClockOutDialog`).
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { UsersRound } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { canSee } from "../../app/navigation";
import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { Stat, StatGrid } from "../../design-system/Stat";
import { Tabs } from "../../design-system/Tabs";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { addDays, formatDate, formatDuration, formatTime, localIsoDate } from "../../lib/format";
import { ClockOutDialog, type OpenSession } from "./ClockOutDialog";
import { CorrectionDialog } from "./CorrectionDialog";
import { DayStatusBadge } from "./DaysTable";
import { ATTENDANCE_KEY, DAY_STATUS_LABELS, STATE_LABELS, formatMinutes } from "./queries";

type Member = components["schemas"]["TeamMemberToday"];
type Entry = components["schemas"]["RegisterEntryView"];
type ReportRow = components["schemas"]["AttendanceReportRow"];
type Ref = components["schemas"]["TeamMemberRef"];

function PersonCell({ person, linked }: { person: Ref; linked: boolean }) {
  return (
    <span className="flex items-center gap-3">
      <Avatar name={person.name} size="sm" />
      <span className="min-w-0">
        {linked ? (
          <Link className="block truncate hover:underline" to={`/employees/${person.id}`}>
            {person.name}
          </Link>
        ) : (
          <span className="block truncate">{person.name}</span>
        )}
        <span className="block text-xs font-normal text-neutral-600">
          {[person.designation, person.department].filter(Boolean).join(" · ")}
        </span>
      </span>
    </span>
  );
}

function Search({ onSearch }: { onSearch: (q: string) => void }) {
  const [text, setText] = useState("");
  return (
    <div className="max-w-md">
      <SearchField
        label="Search people"
        placeholder="Search by name or code"
        value={text}
        onChange={(value) => {
          setText(value);
          if (value === "") onSearch("");
        }}
        onSubmit={(value) => {
          onSearch(value.trim());
        }}
      />
    </div>
  );
}

export function TeamAttendancePage() {
  const me = useMe();
  const linked = canSee(me.data, { anyOf: ["employee.profile.read.team", "employee.profile.read.all"] });
  return (
    <div>
      <PageHeader
        title="Team attendance"
        description="Who is working today, how each day was judged, and totals over a period. Each person's day follows their location's time zone."
      />
      <Tabs
        label="Team attendance"
        items={[
          { id: "today", label: "Today", content: <TodayTab linked={linked} /> },
          { id: "register", label: "Daily register", content: <RegisterTab linked={linked} /> },
          { id: "report", label: "Period report", content: <ReportTab linked={linked} /> },
        ]}
      />
    </div>
  );
}

function TodayTab({ linked }: { linked: boolean }) {
  const me = useMe();
  const canCorrect = hasPermission(me.data, "attendance.correction.create.all");
  const [correcting, setCorrecting] = useState<OpenSession | null>(null);
  const [q, setQ] = useState("");
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
      cell: (row) => <PersonCell person={row.employee} linked={linked} />,
    },
    {
      key: "state",
      header: "Now",
      cell: (row) =>
        row.open_since ? (
          <Badge tone="danger">No clock-out since {formatDate(row.open_since)}</Badge>
        ) : (
          <Badge
            tone={row.state === "clocked_in" ? "success" : row.state === "on_break" ? "warning" : "neutral"}
          >
            {STATE_LABELS[row.state]}
          </Badge>
        ),
    },
    {
      key: "first",
      header: "First in",
      cell: (row) => (row.first_in ? formatTime(row.first_in, row.time_zone) : "None"),
    },
    {
      key: "session",
      header: "Clocked in",
      align: "end",
      // A forgotten clock-out counts only up to the last recorded event.
      cell: (row) => (row.open_since ? "Unknown" : formatDuration(row.session_seconds)),
    },
    { key: "break", header: "Breaks", align: "end", cell: (row) => formatDuration(row.break_seconds) },
    { key: "location", header: "Location", cell: (row) => row.employee.location ?? "Not placed" },
    ...(canCorrect
      ? [
          {
            key: "correct",
            header: "Correction",
            cell: (row: Member) =>
              row.open_since ? (
                <Button
                  size="sm"
                  variant="quiet"
                  onPress={() => {
                    setCorrecting({
                      employeeId: row.employee.id,
                      employeeName: row.employee.name,
                      workDate: row.work_date,
                      timeZone: row.time_zone,
                      clockedInAt: row.first_in,
                      onBreak: row.state === "on_break",
                    });
                  }}
                >
                  Correct clock-out<span className="sr-only"> for {row.employee.name}</span>
                </Button>
              ) : null,
          },
        ]
      : []),
  ];
  return (
    <div className="flex flex-col gap-6">
      {summary ? (
        <StatGrid label="Today's attendance">
          <Stat label="People" value={summary.in_scope} hint="Employed today and visible to you" />
          <Stat label="Clocked in today" value={summary.clocked_in_today} />
          <Stat label="Working now" value={summary.working_now} />
          <Stat label="On a break" value={summary.on_break} />
        </StatGrid>
      ) : null}
      <Search onSearch={setQ} />
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
      {correcting ? (
        <ClockOutDialog
          session={correcting}
          onClose={() => {
            setCorrecting(null);
          }}
        />
      ) : null}
    </div>
  );
}

const STATUS_FILTERS = [
  { id: "all", label: "Every status" },
  ...Object.entries(DAY_STATUS_LABELS).map(([id, label]) => ({ id, label })),
];

function RegisterTab({ linked }: { linked: boolean }) {
  const me = useMe();
  const canRecord = hasPermission(me.data, "attendance.correction.create.all");
  const [date, setDate] = useState(addDays(localIsoDate(), -1));
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [recording, setRecording] = useState<Entry | null>(null);
  const [correcting, setCorrecting] = useState<OpenSession | null>(null);
  const register = useInfiniteQuery({
    queryKey: [...ATTENDANCE_KEY, "register", date, status, q],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/attendance/register", {
          params: {
            query: {
              date,
              ...(status !== "all" ? { status: status as "present" } : {}),
              ...(q ? { q } : {}),
              limit: 50,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: date !== "",
  });
  const first = register.data?.pages[0];
  const rows = register.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<Entry>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (row) => <PersonCell person={row.employee} linked={linked} />,
    },
    { key: "status", header: "Status", cell: (row) => <DayStatusBadge day={row.day} /> },
    { key: "shift", header: "Shift", cell: (row) => row.day.shift_name ?? "None" },
    {
      key: "in",
      header: "First in",
      cell: (row) => (row.day.first_in ? formatTime(row.day.first_in, row.time_zone) : "None"),
    },
    {
      key: "out",
      header: "Last out",
      cell: (row) => (row.day.last_out ? formatTime(row.day.last_out, row.time_zone) : "None"),
    },
    {
      key: "worked",
      header: "Worked",
      align: "end",
      cell: (row) => formatMinutes(row.day.effective_minutes),
    },
    {
      key: "late",
      header: "Late",
      align: "end",
      cell: (row) => (row.day.late_minutes ? formatMinutes(row.day.late_minutes) : "None"),
    },
    ...(canRecord
      ? [
          {
            key: "record",
            header: "Correction",
            cell: (row: Entry) => (
              <span className="flex flex-wrap gap-1">
                {row.day.open_session && row.day.status === "incomplete" ? (
                  <Button
                    size="sm"
                    variant="quiet"
                    onPress={() => {
                      setCorrecting({
                        employeeId: row.employee.id,
                        employeeName: row.employee.name,
                        workDate: row.day.work_date,
                        timeZone: row.time_zone,
                        clockedInAt: row.day.first_in,
                        onBreak: row.day.on_break,
                      });
                    }}
                  >
                    Correct clock-out<span className="sr-only"> for {row.employee.name}</span>
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant="quiet"
                  onPress={() => {
                    setRecording(row);
                  }}
                >
                  Record<span className="sr-only"> a correction for {row.employee.name}</span>
                </Button>
              </span>
            ),
          },
        ]
      : []),
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-[12rem_14rem_1fr] sm:items-end">
        <TextField
          label="Date"
          name="date"
          type="date"
          value={date}
          onChange={setDate}
          max={localIsoDate()}
        />
        <Select label="Status" name="status" options={STATUS_FILTERS} value={status} onChange={setStatus} />
        <Search onSearch={setQ} />
      </div>
      {first ? (
        <p className="text-sm text-neutral-700">
          {first.in_scope} people employed that day.{" "}
          {Object.entries(first.counts)
            .map(
              ([key, count]) =>
                `${key in DAY_STATUS_LABELS ? DAY_STATUS_LABELS[key as keyof typeof DAY_STATUS_LABELS] : key}: ${count}`,
            )
            .join(" · ")}
        </p>
      ) : null}
      {register.isPending ? <Skeleton label="Loading the register" lines={6} /> : null}
      {register.isError ? (
        <ProblemMessage
          error={register.error}
          fallback="We couldn't load the register."
          onRetry={() => void register.refetch()}
        />
      ) : null}
      {register.isSuccess && rows.length === 0 ? (
        <EmptyState icon={UsersRound} title="Nobody to show">
          Nobody in your scope matches this date, status and search.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable
            label="Attendance register"
            columns={columns}
            rows={rows}
            rowKey={(row) => row.employee.id}
          />
          <LoadMore
            shown={rows.length}
            hasMore={register.hasNextPage}
            isLoading={register.isFetchingNextPage}
            onLoad={() => void register.fetchNextPage()}
          />
        </div>
      ) : null}
      {correcting ? (
        <ClockOutDialog
          session={correcting}
          onClose={() => {
            setCorrecting(null);
          }}
        />
      ) : null}
      {recording ? (
        <CorrectionDialog
          day={recording.day}
          workDate={date}
          timeZone={recording.time_zone}
          employeeId={recording.employee.id}
          onClose={() => {
            setRecording(null);
          }}
        />
      ) : null}
    </div>
  );
}

function ReportTab({ linked }: { linked: boolean }) {
  const today = localIsoDate();
  const [from, setFrom] = useState(`${today.slice(0, 7)}-01`);
  const [to, setTo] = useState(today);
  const report = useInfiniteQuery({
    queryKey: [...ATTENDANCE_KEY, "report", from, to],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/attendance/report", {
          params: { query: { from, to, limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: from !== "" && to !== "",
  });
  const rows = report.data?.pages.flatMap((page) => page.items) ?? [];
  const days = (row: ReportRow, key: string) => row.days[key] ?? 0;
  const columns: Column<ReportRow>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (row) => <PersonCell person={row.employee} linked={linked} />,
    },
    { key: "present", header: "Present", align: "end", cell: (row) => days(row, "present") },
    { key: "half", header: "Half days", align: "end", cell: (row) => days(row, "half_day") },
    { key: "absent", header: "Absent", align: "end", cell: (row) => days(row, "absent") },
    { key: "leave", header: "On leave", align: "end", cell: (row) => days(row, "on_leave") },
    { key: "incomplete", header: "No clock-out", align: "end", cell: (row) => days(row, "incomplete") },
    { key: "late", header: "Late days", align: "end", cell: (row) => row.late_days },
    { key: "worked", header: "Worked", align: "end", cell: (row) => formatMinutes(row.effective_minutes) },
    { key: "overtime", header: "Overtime", align: "end", cell: (row) => formatMinutes(row.overtime_minutes) },
    { key: "unjudged", header: "Not judged", align: "end", cell: (row) => row.not_judged_days },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="grid max-w-md gap-3 sm:grid-cols-2">
        <TextField label="From" name="from" type="date" value={from} onChange={setFrom} max={today} />
        <TextField label="To" name="to" type="date" value={to} onChange={setTo} max={today} />
      </div>
      <p className="text-sm text-neutral-600">
        Everyone in your scope employed at some point in the period, at most 62 days. Exports are on the
        Reports page.
      </p>
      {report.isPending ? <Skeleton label="Loading the report" lines={6} /> : null}
      {report.isError ? (
        <ProblemMessage
          error={report.error}
          fallback="We couldn't load the report."
          onRetry={() => void report.refetch()}
        />
      ) : null}
      {report.isSuccess && rows.length === 0 ? (
        <EmptyState icon={UsersRound} title="Nobody to show">
          Nobody in your scope was employed in this period.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable
            label="Attendance report"
            columns={columns}
            rows={rows}
            rowKey={(row) => row.employee.id}
          />
          <LoadMore
            shown={rows.length}
            hasMore={report.hasNextPage}
            isLoading={report.isFetchingNextPage}
            onLoad={() => void report.fetchNextPage()}
          />
        </div>
      ) : null}
    </div>
  );
}
