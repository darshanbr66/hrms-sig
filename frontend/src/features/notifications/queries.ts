/** In-app notifications (product-requirements NTF-1, NTF-3). The badge polls in the background
 * (`X-SV-Background`), so an open tab never keeps an idle session alive. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

export type Notification = components["schemas"]["NotificationView"];
export type Preference = components["schemas"]["PreferenceView"];

export const NOTIFICATIONS_KEY = ["notifications"] as const;
const BACKGROUND = { "X-SV-Background": "1" } as const;

export function useUnreadCount(enabled: boolean) {
  return useQuery({
    queryKey: [...NOTIFICATIONS_KEY, "unread"],
    queryFn: () => unwrap(client.GET("/api/v1/me/notifications/unread-count", { headers: BACKGROUND })),
    enabled,
    refetchInterval: 60_000,
  });
}

export function useMarkRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(
        client.POST("/api/v1/me/notifications/{notification_id}/read", {
          params: { path: { notification_id: id } },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });
}

export function useMarkAllRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/me/notifications/read-all")),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });
}
