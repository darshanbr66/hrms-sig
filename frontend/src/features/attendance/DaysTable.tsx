/**
 * Attendance days: the recorded events, the time between them, and the day as judged by the
 * shift and attendance policy in effect on it, all from the API (CLAUDE.md rule 14). A day
 * without a configured shift and policy shows its times only. Each row opens to its events in
 * time order (DayDetails).
 */
import { CalendarClock } from "lucide-react";

import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import type { components } from "../../lib/api-schema";
import { formatDate, formatDuration, formatTime } from "../../lib/format";
import { DayDetails } from "./DayDetails";
import { ANOMALY_LABELS, DAY_STATUS_LABELS, DAY_STATUS_TONES, formatMinutes } from "./queries";

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

export function DayStatusBadge({ day }: { day: Pick<Day, "status" | "judged"> }) {
  if (day.status) return <Badge tone={DAY_STATUS_TONES[day.status]}>{DAY_STATUS_LABELS[day.status]}</Badge>;
  return <span className="text-neutral-600">{day.judged ? "Not closed yet" : "Not judged"}</span>;
}

export function DaysTable({
  days,
  timeZone,
  label,
  onCorrect,
}: {
  days: Day[];
  timeZone: string;
  label: string;
  /** Offered on each day when the viewer may ask for or record a correction. */
  onCorrect?: (day: Day) => void;
}) {
  if (days.length === 0) {
    return (
      <EmptyState icon={CalendarClock} title="Nothing recorded in this period">
        Days appear here once someone clocks in, or once a shift and attendance policy judge them.
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
    { key: "status", header: "Status", cell: (day) => <DayStatusBadge day={day} /> },
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
      key: "late",
      header: "Late",
      align: "end",
      cell: (day) => (day.late_minutes > 0 ? formatMinutes(day.late_minutes) : "None"),
    },
    {
      key: "notes",
      header: "Notes",
      cell: (day) => {
        const notes = [
          ...(day.open_session && !day.status ? ["No clock-out yet"] : []),
          ...day.anomalies.map((item) => ANOMALY_LABELS[item] ?? item),
          ...(day.overtime_minutes > 0 ? [`Overtime ${formatMinutes(day.overtime_minutes)}`] : []),
          ...(day.events.some((event) => event.voided) ? ["Corrected"] : []),
        ];
        return notes.length > 0 ? notes.join(", ") : <span className="text-neutral-600">None</span>;
      },
    },
    ...(onCorrect
      ? [
          {
            key: "correct",
            header: "Correction",
            cell: (day: Day) => (
              <Button
                size="sm"
                variant="quiet"
                onPress={() => {
                  onCorrect(day);
                }}
              >
                Correct<span className="sr-only"> {formatDate(day.work_date)}</span>
              </Button>
            ),
          },
        ]
      : []),
  ];
  return (
    <div className="flex flex-col gap-2">
      <DataTable
        label={label}
        columns={columns}
        rows={days}
        rowKey={(day) => day.work_date}
        detailsLabel={(day) => formatDate(day.work_date)}
        details={(day) => <DayDetails day={day} timeZone={timeZone} {...(onCorrect ? { onCorrect } : {})} />}
      />
      <p className="text-xs text-neutral-600">Times are shown in {timeZone}.</p>
    </div>
  );
}
