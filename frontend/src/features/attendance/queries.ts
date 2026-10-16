import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { BadgeTone } from "../../design-system/Badge";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

type Schemas = components["schemas"];
export type Today = Schemas["TodayView"];
export type EventType = Today["allowed_events"][number];
export type DayStatus = NonNullable<Schemas["DayView"]["status"]>;
export type CorrectionSummary = Schemas["CorrectionSummary"];
export type CorrectionDetail = Schemas["CorrectionDetailView"];
export type Shift = Schemas["ShiftView"];
export type Policy = Schemas["PolicyView"];

export const ATTENDANCE_KEY = ["attendance"] as const;
export const TODAY_KEY = [...ATTENDANCE_KEY, "today"] as const;

export function useToday(enabled = true) {
  return useQuery({
    queryKey: TODAY_KEY,
    queryFn: () => unwrap(client.GET("/api/v1/me/attendance/today")),
    enabled,
    refetchInterval: 60_000,
  });
}

/** Records one event. Each press gets its own idempotency key, so a retried request (a slow
 * network, a double tap) is recorded once (docs/api-architecture.md §3). */
export function useClock() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (eventType: EventType) =>
      unwrap(
        client.POST("/api/v1/me/attendance/events", {
          params: { header: { "Idempotency-Key": crypto.randomUUID() } },
          body: { event_type: eventType, client_reported_at: new Date().toISOString() },
        }),
      ),
    onSuccess: async (today) => {
      queryClient.setQueryData(TODAY_KEY, today);
      await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
    },
  });
}

export function useShifts(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...ATTENDANCE_KEY, "shifts", includeArchived],
    queryFn: () =>
      unwrap(client.GET("/api/v1/shifts", { params: { query: { include_archived: includeArchived } } })),
    enabled,
  });
}

export const ACTION_LABELS: Record<EventType, string> = {
  CLOCK_IN: "Clock in",
  BREAK_START: "Start break",
  BREAK_END: "End break",
  CLOCK_OUT: "Clock out",
};

export const DONE_LABELS: Record<EventType, string> = {
  CLOCK_IN: "Clocked in",
  BREAK_START: "Break started",
  BREAK_END: "Break ended",
  CLOCK_OUT: "Clocked out",
};

export const STATE_LABELS: Record<Today["state"], string> = {
  clocked_out: "Not clocked in",
  clocked_in: "Clocked in",
  on_break: "On a break",
};

export const DAY_STATUS_LABELS: Record<DayStatus | "not_judged", string> = {
  present: "Present",
  half_day: "Half day",
  absent: "Absent",
  on_leave: "On leave",
  holiday: "Holiday",
  weekly_off: "Weekly off",
  incomplete: "No clock-out",
  pending_correction: "Correction waiting",
  not_judged: "Not judged",
};

export const DAY_STATUS_TONES: Record<DayStatus | "not_judged", BadgeTone> = {
  present: "success",
  half_day: "warning",
  absent: "danger",
  on_leave: "info",
  holiday: "accent",
  weekly_off: "neutral",
  incomplete: "warning",
  pending_correction: "info",
  not_judged: "neutral",
};

export const ANOMALY_LABELS: Record<string, string> = {
  missing_break_end: "Break not ended",
  break_exceeded: "Breaks over the limit",
  holiday_work: "Worked on a holiday",
  weekly_off_work: "Worked on a weekly off",
  leave_day_work: "Worked on a leave day",
  half_day_leave: "Half-day leave",
  overtime_needs_approval: "Overtime needs approval",
};

export const CORRECTION_STATUS_LABELS: Record<CorrectionSummary["status"], string> = {
  pending: "Waiting for a decision",
  approved: "Approved",
  rejected: "Not approved",
  cancelled: "Withdrawn",
};

export const CORRECTION_STATUS_TONES: Record<CorrectionSummary["status"], BadgeTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
  cancelled: "neutral",
};

export const WEEKDAYS = [
  { id: "1", label: "Monday" },
  { id: "2", label: "Tuesday" },
  { id: "3", label: "Wednesday" },
  { id: "4", label: "Thursday" },
  { id: "5", label: "Friday" },
  { id: "6", label: "Saturday" },
  { id: "7", label: "Sunday" },
];

/** "45 min" or "2h 05m" from whole minutes. */
export function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** "Sundays; 2nd and 4th Saturdays" from a shift version's weekly offs. */
export function describeWeeklyOffs(offs: readonly { weekday: number; weeks: number[] | null }[]): string {
  if (offs.length === 0) return "No weekly offs";
  const ordinal = (n: number) => `${n}${n === 1 ? "st" : n === 2 ? "nd" : n === 3 ? "rd" : "th"}`;
  return offs
    .map((off) => {
      const day = WEEKDAYS[off.weekday - 1]?.label ?? "";
      return off.weeks ? `${off.weeks.map(ordinal).join(" and ")} ${day}s` : `${day}s`;
    })
    .join("; ");
}
