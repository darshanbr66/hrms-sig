/**
 * A month of attendance at a glance: each date carries the status the API judged for it, in
 * words where there is room and always in the legend, never colour alone. Choosing a date
 * opens its events (DayDetails); the table view lists the same days. Built on React Aria's
 * Calendar for the grid semantics, arrow-key movement and month buttons.
 */
import { parseDate, type CalendarDate } from "@internationalized/date";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import {
  Button as AriaButton,
  Calendar,
  CalendarCell,
  CalendarGrid,
  CalendarGridBody,
  CalendarGridHeader,
  CalendarHeaderCell,
  Heading,
} from "react-aria-components";

import type { BadgeTone } from "../../design-system/Badge";
import type { Day } from "./DaysTable";
import { DAY_STATUS_LABELS, DAY_STATUS_TONES } from "./queries";

const CELL_TONES: Record<BadgeTone, string> = {
  neutral: "bg-neutral-100 text-neutral-700",
  success: "bg-success-50 text-success-700",
  warning: "bg-warning-50 text-warning-700",
  danger: "bg-danger-50 text-danger-700",
  info: "bg-info-50 text-info-700",
  accent: "bg-accent-50 text-accent-700",
};

const DOT_TONES: Record<BadgeTone, string> = {
  neutral: "bg-neutral-500",
  success: "bg-success-700",
  warning: "bg-warning-700",
  danger: "bg-danger-700",
  info: "bg-info-700",
  accent: "bg-accent-600",
};

function statusOf(day: Day | undefined): keyof typeof DAY_STATUS_LABELS | null {
  if (!day) return null;
  return day.status ?? (day.judged ? null : "not_judged");
}

export function AttendanceMonth({
  days,
  month,
  onMonthChange,
  selected,
  onSelect,
  today,
}: {
  days: readonly Day[];
  /** Any date in the month shown. */
  month: string;
  onMonthChange: (firstOfMonth: string) => void;
  selected: string | null;
  onSelect: (date: string) => void;
  today: string;
}) {
  // The focused date is the calendar's own (arrow keys move it day by day); the month shown
  // follows it, and the page fetches that month's days.
  const [focused, setFocused] = useState(() =>
    parseDate(selected ?? (today.slice(0, 7) === month.slice(0, 7) ? today : month)),
  );
  const byDate = new Map(days.map((day) => [day.work_date, day]));
  const shown = new Set(days.map((day) => statusOf(day)).filter((status) => status !== null));
  return (
    <div className="flex flex-col gap-3">
      <Calendar
        aria-label="Attendance by day"
        value={selected ? parseDate(selected) : null}
        onChange={(value: CalendarDate) => {
          onSelect(value.toString());
        }}
        focusedValue={focused}
        onFocusChange={(value: CalendarDate) => {
          setFocused(value);
          const first = `${value.toString().slice(0, 7)}-01`;
          if (first !== `${month.slice(0, 7)}-01`) onMonthChange(first);
        }}
        maxValue={parseDate(today)}
        className="w-full max-w-3xl"
      >
        <header className="mb-3 flex items-center gap-2">
          <AriaButton
            slot="previous"
            className="rounded-[var(--radius-control)] p-2 text-neutral-700 hover:bg-neutral-100"
          >
            <ChevronLeft aria-hidden className="size-4" />
          </AriaButton>
          <Heading className="min-w-40 flex-1 text-center text-base font-semibold text-neutral-900" />
          <AriaButton
            slot="next"
            className="rounded-[var(--radius-control)] p-2 text-neutral-700 hover:bg-neutral-100 disabled:opacity-40"
          >
            <ChevronRight aria-hidden className="size-4" />
          </AriaButton>
        </header>
        {/* A new month fades in, so the change of range is noticed (none with reduced motion). */}
        <div key={month.slice(0, 7)} className="animate-reveal">
          <CalendarGrid className="w-full table-fixed border-separate border-spacing-1">
            <CalendarGridHeader>
              {(weekday) => (
                <CalendarHeaderCell className="pb-1 text-xs font-semibold text-neutral-600">
                  {weekday}
                </CalendarHeaderCell>
              )}
            </CalendarGridHeader>
            <CalendarGridBody>
              {(date) => {
                const iso = date.toString();
                const status = statusOf(byDate.get(iso));
                const tone = status ? DAY_STATUS_TONES[status] : null;
                return (
                  <CalendarCell
                    date={date}
                    data-status={status ?? undefined}
                    className={({ isSelected, isOutsideMonth, isDisabled }) =>
                      `flex h-14 cursor-default flex-col items-start justify-between rounded-[var(--radius-control)] border p-1.5 text-left outline-none sm:h-16 ${
                        isOutsideMonth ? "invisible" : ""
                      } ${tone ? CELL_TONES[tone] : "bg-neutral-0 text-neutral-500"} ${
                        isSelected ? "border-accent-600 ring-2 ring-accent-600" : "border-neutral-200"
                      } ${isDisabled ? "opacity-40" : "hover:border-neutral-500"} data-[focus-visible]:outline-2 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-accent-600`
                    }
                  >
                    {({ formattedDate }) => (
                      <>
                        <span className="tabular text-sm font-semibold">{formattedDate}</span>
                        {status ? (
                          <span className="flex w-full min-w-0 items-center gap-1">
                            <span
                              aria-hidden
                              className={`size-1.5 shrink-0 rounded-full ${tone ? DOT_TONES[tone] : ""}`}
                            />
                            <span className="hidden truncate text-[0.6875rem] leading-tight font-semibold md:inline">
                              {DAY_STATUS_LABELS[status]}
                            </span>
                          </span>
                        ) : null}
                      </>
                    )}
                  </CalendarCell>
                );
              }}
            </CalendarGridBody>
          </CalendarGrid>
        </div>
      </Calendar>
      {shown.size > 0 ? (
        <ul aria-label="Key" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-neutral-700">
          {[...shown].map((status) => (
            <li key={status} className="flex items-center gap-1.5">
              <span aria-hidden className={`size-2 rounded-full ${DOT_TONES[DAY_STATUS_TONES[status]]}`} />
              {DAY_STATUS_LABELS[status]}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
