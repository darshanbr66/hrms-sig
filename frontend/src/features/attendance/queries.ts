import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

export type Today = components["schemas"]["TodayView"];
export type EventType = Today["allowed_events"][number];

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
