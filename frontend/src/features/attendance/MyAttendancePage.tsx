/** The employee's own attendance: today's clock, the days of a chosen period as judged by their
 * shift and attendance policy, and their correction requests (ATT-7). */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { CalendarDays, ClipboardPen, Rows3 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs } from "../../design-system/Tabs";
import { ViewSwitch } from "../../design-system/ViewSwitch";
import { client, unwrap } from "../../lib/api";
import { addDays, formatDate, localIsoDate } from "../../lib/format";
import { AttendanceMonth } from "./AttendanceMonth";
import { ClockCard } from "./ClockCard";
import { CorrectionDialog } from "./CorrectionDialog";
import { DayDetails } from "./DayDetails";
import { DaysTable, type Day } from "./DaysTable";
import {
  ATTENDANCE_KEY,
  CORRECTION_STATUS_LABELS,
  CORRECTION_STATUS_TONES,
  type CorrectionSummary,
} from "./queries";

function periods(today: string) {
  const [year, month] = today.split("-").map(Number);
  const firstOfMonth = `${today.slice(0, 7)}-01`;
  const previous = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 2, 1)).toISOString().slice(0, 10);
  return [
    { id: "7", label: "Last 7 days", from: addDays(today, -6), to: today },
    { id: "month", label: "This month", from: firstOfMonth, to: today },
    { id: "previous", label: "Last month", from: previous, to: addDays(firstOfMonth, -1) },
    { id: "31", label: "Last 31 days", from: addDays(today, -30), to: today },
  ];
}

export function MyAttendancePage() {
  const me = useMe();
  const canCorrect = hasPermission(me.data, "attendance.correction.request.self");
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="My attendance"
        description="Clock in and out, take breaks, and see how your days were judged."
      />
      <div className="max-w-2xl">
        <ClockCard />
      </div>
      <Tabs
        label="My attendance"
        items={[
          { id: "days", label: "Days", content: <MyDays canCorrect={canCorrect} /> },
          ...(canCorrect ? [{ id: "corrections", label: "My corrections", content: <MyCorrections /> }] : []),
        ]}
      />
    </div>
  );
}

function lastOfMonth(firstOfMonth: string): string {
  const [year, month] = firstOfMonth.split("-").map(Number);
  return new Date(Date.UTC(year ?? 1970, month ?? 1, 0)).toISOString().slice(0, 10);
}

function MyDays({ canCorrect }: { canCorrect: boolean }) {
  const today = localIsoDate();
  const choices = periods(today);
  const [view, setView] = useState<"table" | "month">("table");
  const [period, setPeriod] = useState("month");
  const [month, setMonth] = useState(`${today.slice(0, 7)}-01`);
  const [selected, setSelected] = useState<string | null>(null);
  const [correcting, setCorrecting] = useState<{ day: Day | null } | null>(null);
  const range = choices.find((choice) => choice.id === period) ?? choices[1];
  const monthEnd = lastOfMonth(month);
  const from = view === "month" ? month : (range?.from ?? today);
  const to = view === "month" ? (monthEnd < today ? monthEnd : today) : (range?.to ?? today);
  const days = useQuery({
    queryKey: [...ATTENDANCE_KEY, "days", from, to],
    queryFn: () => unwrap(client.GET("/api/v1/me/attendance/days", { params: { query: { from, to } } })),
  });
  const unjudged = days.data?.items.some((day) => !day.judged) ?? false;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <ViewSwitch
            label="Show days as"
            options={[
              { id: "table", label: "Table", icon: Rows3 },
              { id: "month", label: "Month", icon: CalendarDays },
            ]}
            value={view}
            onChange={setView}
          />
          {view === "table" ? (
            <div className="w-full max-w-xs">
              <Select label="Period" name="period" options={choices} value={period} onChange={setPeriod} />
            </div>
          ) : null}
        </div>
        {canCorrect && days.isSuccess ? (
          <Button
            variant="secondary"
            onPress={() => {
              setCorrecting({ day: null });
            }}
          >
            <ClipboardPen aria-hidden className="size-4" />
            Correct another date
          </Button>
        ) : null}
      </div>
      {unjudged ? (
        <Banner tone="info">
          Some days are not judged because no shift or attendance policy applies to you on them. Ask HR if you
          expected one.
        </Banner>
      ) : null}
      {days.isPending ? <Skeleton label="Loading your attendance" lines={5} /> : null}
      {days.isError ? (
        <ProblemMessage
          error={days.error}
          fallback="We couldn't load your attendance."
          onRetry={() => void days.refetch()}
        />
      ) : null}
      {days.isSuccess && view === "month" ? (
        <div className="flex flex-col gap-4">
          <AttendanceMonth
            days={days.data.items}
            month={month}
            onMonthChange={(first) => {
              setMonth(first);
              setSelected(null);
            }}
            selected={selected}
            onSelect={setSelected}
            today={today}
          />
          {selected ? (
            <div className="animate-reveal rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-4">
              <h2 className="mb-2 text-base font-semibold text-neutral-900">{formatDate(selected)}</h2>
              {(() => {
                const day = days.data.items.find((item) => item.work_date === selected);
                return day ? (
                  <DayDetails
                    day={day}
                    timeZone={days.data.time_zone}
                    {...(canCorrect
                      ? {
                          onCorrect: (chosen: Day) => {
                            setCorrecting({ day: chosen });
                          },
                        }
                      : {})}
                  />
                ) : (
                  <p className="text-sm text-neutral-600">Nothing was recorded or judged on this day.</p>
                );
              })()}
            </div>
          ) : (
            <p className="text-sm text-neutral-600">Choose a date to see its events.</p>
          )}
        </div>
      ) : null}
      {days.isSuccess && view === "table" ? (
        <DaysTable
          days={days.data.items}
          timeZone={days.data.time_zone}
          label="Your attendance days"
          {...(canCorrect
            ? {
                onCorrect: (day: Day) => {
                  setCorrecting({ day });
                },
              }
            : {})}
        />
      ) : null}
      {correcting && days.isSuccess ? (
        <CorrectionDialog
          day={correcting.day}
          workDate={correcting.day?.work_date ?? null}
          timeZone={days.data.time_zone}
          onClose={() => {
            setCorrecting(null);
          }}
        />
      ) : null}
    </div>
  );
}

export function CorrectionList({
  label,
  rows,
  showEmployee,
}: {
  label: string;
  rows: CorrectionSummary[];
  showEmployee: boolean;
}) {
  const columns: Column<CorrectionSummary>[] = [
    {
      key: "date",
      header: "Work date",
      primary: true,
      cell: (row) => (
        <Link to={`/attendance/corrections/${row.id}`} className="hover:underline">
          {formatDate(row.work_date)}
        </Link>
      ),
    },
    ...(showEmployee
      ? [{ key: "employee", header: "Employee", cell: (row: CorrectionSummary) => row.employee_name }]
      : []),
    {
      key: "kind",
      header: "Made by",
      cell: (row) => (row.kind === "hr_recorded" ? "HR" : "Employee"),
    },
    {
      key: "status",
      header: "Status",
      cell: (row) => (
        <span className="flex flex-wrap gap-1">
          <Badge tone={CORRECTION_STATUS_TONES[row.status]}>{CORRECTION_STATUS_LABELS[row.status]}</Badge>
          {row.locked_period ? <Badge tone="neutral">Locked period</Badge> : null}
        </span>
      ),
    },
    {
      key: "asked",
      header: "Asked on",
      cell: (row) => formatDate(row.requested_at.slice(0, 10), { weekday: false }),
    },
  ];
  return <DataTable label={label} columns={columns} rows={rows} rowKey={(row) => row.id} />;
}

function MyCorrections() {
  const list = useInfiniteQuery({
    queryKey: [...ATTENDANCE_KEY, "corrections", "me"],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/me/attendance/corrections", {
          params: { query: { limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  if (list.isPending) return <Skeleton label="Loading your corrections" lines={3} />;
  if (list.isError) {
    return (
      <ProblemMessage
        error={list.error}
        fallback="We couldn't load your corrections."
        onRetry={() => void list.refetch()}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState icon={ClipboardPen} title="No corrections yet">
        If a day is wrong, for example a forgotten clock-out, choose Correct on that day.
      </EmptyState>
    );
  }
  return (
    <>
      <CorrectionList label="My corrections" rows={rows} showEmployee={false} />
      <LoadMore
        shown={rows.length}
        hasMore={list.hasNextPage}
        isLoading={list.isFetchingNextPage}
        onLoad={() => void list.fetchNextPage()}
      />
    </>
  );
}
