/**
 * Recorded attendance days: the events and the time between them, as measured by the API.
 * There is no present/late/absent status here: that judgement needs Sigvitas's shift and
 * attendance policy, which are not configured yet.
 */
import { CalendarClock } from "lucide-react";

import { Badge } from "../../design-system/Badge";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import type { components } from "../../lib/api-schema";
import { formatDate, formatDuration, formatTime } from "../../lib/format";

export type Day = components["schemas"]["DayView"];

const EVENT_LABELS: Record<Day["events"][number]["event_type"], string> = {
  CLOCK_IN: "Clocked in",
  BREAK_START: "Break started",
  BREAK_END: "Break ended",
  CLOCK_OUT: "Clocked out",
};

export function eventLabel(type: Day["events"][number]["event_type"]): string {
  return EVENT_LABELS[type];
}

export function DaysTable({ days, timeZone, label }: { days: Day[]; timeZone: string; label: string }) {
  if (days.length === 0) {
    return (
      <EmptyState icon={CalendarClock} title="No attendance recorded in this period">
        Days appear here once someone clocks in.
      </EmptyState>
    );
  }
  const columns: Column<Day>[] = [
    {
      key: "date",
      header: "Date",
      primary: true,
      cell: (day) => formatDate(day.work_date, { year: false }),
    },
    {
      key: "in",
      header: "First in",
      cell: (day) => (day.first_in ? formatTime(day.first_in, timeZone) : "None"),
    },
    {
      key: "out",
      header: "Last out",
      cell: (day) =>
        day.last_out ? formatTime(day.last_out, timeZone) : day.open_session ? "Still in" : "None",
    },
    {
      key: "session",
      header: "Clocked in",
      align: "end",
      cell: (day) => formatDuration(day.session_seconds),
    },
    { key: "break", header: "Breaks", align: "end", cell: (day) => formatDuration(day.break_seconds) },
    {
      key: "notes",
      header: "Notes",
      cell: (day) =>
        day.open_session ? (
          <Badge tone="warning">No clock-out yet</Badge>
        ) : day.missing_break_end ? (
          <Badge tone="warning">Break not ended</Badge>
        ) : (
          <span className="text-neutral-500">None</span>
        ),
    },
  ];
  return (
    <div className="flex flex-col gap-2">
      <DataTable label={label} columns={columns} rows={days} rowKey={(day) => day.work_date} />
      <p className="text-xs text-neutral-600">Times are shown in {timeZone}.</p>
    </div>
  );
}
