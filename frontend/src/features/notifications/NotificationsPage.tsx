/** The signed-in person's notifications and which ones they receive (NTF-1, NTF-3). Security
 * emails (sign-in, MFA, password) are not listed: they are always sent. */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellOff } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Button } from "../../design-system/Button";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs } from "../../design-system/Tabs";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDateTime, formatRelative } from "../../lib/format";
import {
  NOTIFICATIONS_KEY,
  useMarkAllRead,
  useMarkRead,
  type Notification,
  type Preference,
} from "./queries";

export function NotificationsPage() {
  return (
    <div>
      <PageHeader
        title="Notifications"
        description="Things that need your attention or that changed for you. Notifications never include reasons, notes or document contents."
      />
      <Tabs
        label="Notifications"
        items={[
          { id: "inbox", label: "Inbox", content: <Inbox /> },
          { id: "preferences", label: "What I receive", content: <Preferences /> },
        ]}
      />
    </div>
  );
}

function Inbox() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const list = useInfiniteQuery({
    queryKey: [...NOTIFICATIONS_KEY, "list", unreadOnly],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/me/notifications", {
          params: { query: { unread: unreadOnly, limit: 30, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const markAll = useMarkAllRead();
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  const unread = list.data?.pages[0]?.unread_count ?? 0;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Checkbox isSelected={unreadOnly} onChange={setUnreadOnly}>
          Show unread only
        </Checkbox>
        {unread > 0 ? (
          <Button
            variant="secondary"
            size="sm"
            onPress={() => {
              markAll.mutate();
            }}
            isPending={markAll.isPending}
          >
            Mark all {unread} as read
          </Button>
        ) : null}
      </div>
      {list.isPending ? <Skeleton label="Loading notifications" lines={5} /> : null}
      {list.isError ? (
        <ProblemMessage
          error={list.error}
          fallback="We couldn't load your notifications."
          onRetry={() => void list.refetch()}
        />
      ) : null}
      {list.isSuccess && rows.length === 0 ? (
        <EmptyState icon={BellOff} title={unreadOnly ? "Nothing unread" : "No notifications yet"}>
          Requests waiting for you, decisions on yours and finished exports appear here.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <>
          <ul
            aria-label="Notifications"
            className="flex flex-col divide-y divide-neutral-200 rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0"
          >
            {rows.map((row) => (
              <NotificationRow key={row.id} item={row} />
            ))}
          </ul>
          <LoadMore
            shown={rows.length}
            hasMore={list.hasNextPage}
            isLoading={list.isFetchingNextPage}
            onLoad={() => void list.fetchNextPage()}
          />
        </>
      ) : null}
    </div>
  );
}

function NotificationRow({ item }: { item: Notification }) {
  const markRead = useMarkRead();
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () =>
      unwrap(
        client.DELETE("/api/v1/me/notifications/{notification_id}", {
          params: { path: { notification_id: item.id } },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });
  return (
    <li className={`flex flex-wrap items-start gap-3 px-4 py-3 ${item.read ? "" : "bg-accent-50/60"}`}>
      <span
        aria-hidden
        className={`mt-1.5 size-2 shrink-0 rounded-full ${item.read ? "bg-transparent" : "bg-accent-600"}`}
      />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-neutral-900">
          {item.read ? null : <span className="sr-only">Unread: </span>}
          {item.link ? (
            <Link
              to={item.link}
              className="hover:underline"
              onClick={() => {
                if (!item.read) markRead.mutate(item.id);
              }}
            >
              {item.title}
            </Link>
          ) : (
            item.title
          )}
        </p>
        {item.body ? <p className="mt-0.5 text-sm text-neutral-700">{item.body}</p> : null}
        <p className="mt-1 text-xs text-neutral-500" title={formatDateTime(item.created_at)}>
          {formatRelative(item.created_at)}
        </p>
      </div>
      <div className="flex gap-1">
        {item.read ? null : (
          <Button
            variant="quiet"
            size="sm"
            onPress={() => {
              markRead.mutate(item.id);
            }}
          >
            Mark read<span className="sr-only">: {item.title}</span>
          </Button>
        )}
        <Button
          variant="quiet"
          size="sm"
          onPress={() => {
            remove.mutate();
          }}
          isPending={remove.isPending}
        >
          Remove<span className="sr-only">: {item.title}</span>
        </Button>
      </div>
    </li>
  );
}

const CHANNEL_LABELS: Record<string, string> = { in_app: "In the app", email: "By email" };

const PREFERENCES_KEY = [...NOTIFICATIONS_KEY, "preferences"] as const;
const SAVE_PREFERENCE_KEY = [...PREFERENCES_KEY, "save"] as const;

type PreferenceList = { items: Preference[] };
type PreferenceChange = { type: Preference["type"]; channel: "in_app" | "email"; enabled: boolean };

function withChange(list: PreferenceList, change: PreferenceChange): PreferenceList {
  return {
    ...list,
    items: list.items.map((item) =>
      item.type === change.type
        ? { ...item, channels: { ...item.channels, [change.channel]: change.enabled } }
        : item,
    ),
  };
}

/**
 * Each checkbox shows the new state at once, the change is sent, and the saved list the API
 * returns becomes the truth. A failed save puts the box back and says so; nothing reports
 * success unless the API confirmed it. While several changes are in flight only the last
 * answer is applied, so a slow early answer cannot undo a later click.
 */
function Preferences() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const prefs = useQuery({
    queryKey: PREFERENCES_KEY,
    queryFn: () => unwrap(client.GET("/api/v1/me/notification-preferences")),
  });
  const save = useMutation({
    mutationKey: SAVE_PREFERENCE_KEY,
    mutationFn: (change: PreferenceChange) =>
      unwrap(client.PUT("/api/v1/me/notification-preferences", { body: { changes: [change] } })),
    onMutate: async (change) => {
      await queryClient.cancelQueries({ queryKey: PREFERENCES_KEY });
      const previous = queryClient.getQueryData<PreferenceList>(PREFERENCES_KEY);
      if (previous) queryClient.setQueryData(PREFERENCES_KEY, withChange(previous, change));
      return { previous };
    },
    onError: (_error, change) => {
      // Undo only this change; later clicks on other boxes stay as they are.
      const current = queryClient.getQueryData<PreferenceList>(PREFERENCES_KEY);
      if (current)
        queryClient.setQueryData(
          PREFERENCES_KEY,
          withChange(current, { ...change, enabled: !change.enabled }),
        );
    },
    onSuccess: (data) => {
      if (queryClient.isMutating({ mutationKey: SAVE_PREFERENCE_KEY }) === 1) {
        queryClient.setQueryData(PREFERENCES_KEY, data);
      }
      toast("Preference saved");
    },
    onSettled: async () => {
      if (queryClient.isMutating({ mutationKey: SAVE_PREFERENCE_KEY }) === 1) {
        await queryClient.invalidateQueries({ queryKey: PREFERENCES_KEY });
      }
    },
  });
  if (prefs.isPending) return <Skeleton label="Loading your preferences" lines={4} />;
  if (prefs.isError) {
    return (
      <ProblemMessage
        error={prefs.error}
        fallback="We couldn't load your preferences."
        onRetry={() => void prefs.refetch()}
      />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-prose text-sm text-neutral-600">
        Turn off what you do not want. Approvals still wait in their inbox whatever you choose here. Emails
        about your sign-in and security are always sent.
      </p>
      {save.isError ? (
        <ProblemMessage error={save.error} fallback="We couldn't save the preference." />
      ) : null}
      <ul aria-label="Notification preferences" className="flex flex-col gap-3">
        {prefs.data.items.map((item) => (
          <li
            key={item.type}
            className="rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 px-4 py-3"
          >
            <p className="font-semibold text-neutral-900">{item.label}</p>
            <p className="text-sm text-neutral-600">{item.description}</p>
            <div className="mt-2 flex flex-wrap gap-4">
              {Object.entries(item.channels).map(([channel, enabled]) => (
                <Checkbox
                  key={channel}
                  isSelected={enabled}
                  onChange={(next) => {
                    save.mutate({ type: item.type, channel: channel as "in_app" | "email", enabled: next });
                  }}
                >
                  {CHANNEL_LABELS[channel] ?? channel}
                </Checkbox>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
