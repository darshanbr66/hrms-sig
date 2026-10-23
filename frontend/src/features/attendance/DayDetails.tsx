/**
 * One attendance day opened up: every recorded event in time order, with removed (voided) and
 * correction-added events marked, the shift the day was judged against and the figures the
 * API derived (CLAUDE.md rule 14: nothing is calculated here). The summary rows and the
 * calendar only point to this; the events themselves are always reachable.
 */
import { ClipboardPen, LogIn, LogOut, Pause, Play } from "lucide-react";

import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { formatDate, formatDuration, formatTime } from "../../lib/format";
import { eventLabel, DayStatusBadge, type Day } from "./DaysTable";
import { OUTCOME_LABELS } from "./locationText";
import { ANOMALY_LABELS, formatMinutes } from "./queries";

type DayEvent = Day["events"][number];

const EVENT_ICONS: Record<DayEvent["event_type"], typeof LogIn> = {
  CLOCK_IN: LogIn,
  BREAK_START: Pause,
  BREAK_END: Play,
  CLOCK_OUT: LogOut,
};

const SOURCE_LABELS: Record<DayEvent["source"], string> = {
  web: "Web",
  mobile: "Mobile",
  correction: "Correction",
  import: "Import",
};

export function DayDetails({
  day,
  timeZone,
  onCorrect,
}: {
  day: Day;
  timeZone: string;
  onCorrect?: (day: Day) => void;
}) {
  // Time order for reading; the API's records are not changed or recounted.
  const events = [...day.events].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
  const figures = [
    { label: "Clocked in", value: formatDuration(day.session_seconds) },
    { label: "Breaks", value: formatDuration(day.break_seconds) },
    ...(day.judged ? [{ label: "Counted as worked", value: formatMinutes(day.effective_minutes) }] : []),
    ...(day.late_minutes > 0 ? [{ label: "Late", value: formatMinutes(day.late_minutes) }] : []),
    ...(day.early_leave_minutes > 0
      ? [{ label: "Left early", value: formatMinutes(day.early_leave_minutes) }]
      : []),
    ...(day.overtime_minutes > 0 ? [{ label: "Overtime", value: formatMinutes(day.overtime_minutes) }] : []),
  ];
  return (
    <section
      aria-label={`Details for ${formatDate(day.work_date)}`}
      className="flex flex-col gap-4 pt-1 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <DayStatusBadge day={day} />
        {day.holiday ? <Badge tone="accent">Holiday</Badge> : null}
        {Number(day.leave_portion) > 0 ? (
          <Badge tone="info">{Number(day.leave_portion) < 1 ? "Half day of leave" : "On leave"}</Badge>
        ) : null}
        {day.anomalies.map((item) => (
          <Badge key={item} tone="warning">
            {ANOMALY_LABELS[item] ?? item}
          </Badge>
        ))}
        <span className="text-neutral-600">
          {day.shift_name
            ? `Shift ${day.shift_name}${
                day.shift_start_at && day.shift_end_at
                  ? `, ${formatTime(day.shift_start_at, timeZone)} to ${formatTime(day.shift_end_at, timeZone)}`
                  : ""
              }`
            : "No shift applied"}
        </span>
      </div>
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,16rem)]">
        {events.length === 0 ? (
          <p className="text-neutral-600">No clock events were recorded on this day.</p>
        ) : (
          <ol aria-label={`Events on ${formatDate(day.work_date)}`} className="flex flex-col">
            {events.map((event, index) => {
              const Icon = EVENT_ICONS[event.event_type];
              const last = index === events.length - 1;
              return (
                <li key={event.id} className="relative flex gap-3 pb-3 last:pb-0">
                  {last ? null : (
                    <span aria-hidden className="absolute top-7 bottom-0 left-3.5 w-px bg-neutral-300" />
                  )}
                  <span
                    aria-hidden
                    className={`relative flex size-7 shrink-0 items-center justify-center rounded-full border ${
                      event.voided
                        ? "border-dashed border-neutral-500 bg-neutral-0 text-neutral-600"
                        : "border-accent-600 bg-accent-50 text-accent-700"
                    }`}
                  >
                    <Icon className="size-3.5" />
                  </span>
                  <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-1 pt-1">
                    <span
                      className={`tabular font-semibold ${event.voided ? "text-neutral-600 line-through" : "text-neutral-900"}`}
                    >
                      {formatTime(event.occurred_at, timeZone)}
                    </span>
                    <span className={event.voided ? "text-neutral-600 line-through" : "text-neutral-800"}>
                      {eventLabel(event.event_type)}
                    </span>
                    {event.voided ? <Badge tone="neutral">Removed by a correction</Badge> : null}
                    {!event.voided && event.correction_id ? (
                      <Badge tone="info">Added by a correction</Badge>
                    ) : null}
                    {event.location ? (
                      <Badge tone={OUTCOME_LABELS[event.location.outcome].tone}>
                        {OUTCOME_LABELS[event.location.outcome].label}
                      </Badge>
                    ) : null}
                    <span className="text-xs text-neutral-600">{SOURCE_LABELS[event.source]}</span>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 self-start rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-3">
          {figures.map((item) => (
            <div key={item.label} className="min-w-0">
              <dt className="text-xs font-semibold text-neutral-600">{item.label}</dt>
              <dd className="tabular text-neutral-900">{item.value}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-neutral-600">Times are shown in {timeZone}.</p>
        {onCorrect ? (
          <Button
            size="sm"
            variant="secondary"
            onPress={() => {
              onCorrect(day);
            }}
          >
            <ClipboardPen aria-hidden className="size-3.5" />
            Correct this day<span className="sr-only">, {formatDate(day.work_date)}</span>
          </Button>
        ) : null}
      </div>
    </section>
  );
}
