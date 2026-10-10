/**
 * Today's attendance with the one action that comes next (ui-ux-guidelines.md §2, Home). The
 * server's time is the event time; the running total shown between refreshes adds the seconds
 * since the server's answer to the server's own figures, so nothing is calculated here.
 */
import { Clock, Coffee, LogIn, LogOut } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { formatDate, formatDuration, formatTime } from "../../lib/format";
import { problemText } from "../../lib/problem";
import { eventLabel } from "./DaysTable";
import {
  ACTION_LABELS,
  DONE_LABELS,
  STATE_LABELS,
  useClock,
  useToday,
  type EventType,
  type Today,
} from "./queries";

const ICONS: Record<EventType, typeof Clock> = {
  CLOCK_IN: LogIn,
  BREAK_START: Coffee,
  BREAK_END: Clock,
  CLOCK_OUT: LogOut,
};

function useElapsedSince(serverTime: string | undefined, running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 30_000);
    return () => {
      window.clearInterval(timer);
    };
  }, [running]);
  if (!running || !serverTime) return 0;
  return Math.max(0, Math.floor((now - new Date(serverTime).getTime()) / 1000));
}

export function ClockCard({ compact = false }: { compact?: boolean }) {
  const today = useToday();
  const clock = useClock();
  const [announcement, setAnnouncement] = useState("");
  const data = today.data;
  const running = data?.state === "clocked_in" || data?.state === "on_break";
  const elapsed = useElapsedSince(data?.server_time, running);

  const press = (eventType: EventType) => {
    clock.mutate(eventType, {
      onSuccess: (result) => {
        setAnnouncement(`${DONE_LABELS[eventType]} at ${formatTime(result.server_time, result.time_zone)}.`);
      },
    });
  };

  return (
    <Panel
      title="Today"
      description={data ? `${formatDate(data.work_date)} · ${data.time_zone}` : undefined}
      action={data ? <StateBadge state={data.state} /> : undefined}
    >
      <p role="status" className="sr-only">
        {announcement}
      </p>
      {today.isPending ? <Skeleton label="Loading today's attendance" /> : null}
      {today.isError ? (
        <ProblemMessage
          error={today.error}
          fallback="We couldn't load today's attendance."
          onRetry={() => void today.refetch()}
        />
      ) : null}
      {data ? (
        <div className="flex flex-col gap-4">
          {data.open_since ? (
            <Banner tone="warning">
              You clocked in on {formatDate(data.open_since)} and have not clocked out. Clocking out now
              records the current time; ask HR to correct that day if it is wrong.
            </Banner>
          ) : null}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-sm text-neutral-600">Clocked in</p>
              <p className="tabular text-[1.75rem] font-semibold leading-tight text-neutral-900">
                {formatDuration(data.day.session_seconds + (running ? elapsed : 0))}
              </p>
            </div>
            <div>
              <p className="text-sm text-neutral-600">Breaks</p>
              <p className="tabular text-[1.75rem] font-semibold leading-tight text-neutral-900">
                {formatDuration(data.day.break_seconds + (data.state === "on_break" ? elapsed : 0))}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {data.allowed_events.map((eventType, index) => {
              const Icon = ICONS[eventType];
              return (
                <Button
                  key={eventType}
                  variant={index === 0 ? "primary" : "secondary"}
                  isPending={clock.isPending && clock.variables === eventType}
                  isDisabled={clock.isPending}
                  onPress={() => {
                    press(eventType);
                  }}
                >
                  <Icon aria-hidden className="size-4" />
                  {ACTION_LABELS[eventType]}
                </Button>
              );
            })}
          </div>
          {clock.isError ? (
            <Banner tone="danger">{problemText(clock.error, "We couldn't record that. Try again.")}</Banner>
          ) : null}
          {!compact && data.day.events.length > 0 ? <Timeline today={data} /> : null}
          {compact && data.day.events.length > 0 ? (
            <p className="text-sm text-neutral-600">
              First in at {formatTime(data.day.events[0]?.occurred_at ?? data.server_time, data.time_zone)}
              {data.day.last_out ? `, last out at ${formatTime(data.day.last_out, data.time_zone)}` : ""}.
            </p>
          ) : null}
          <p className="text-xs text-neutral-500">
            Times are recorded by the server. Signing in to the HRMS does not clock you in.
          </p>
        </div>
      ) : null}
    </Panel>
  );
}

function StateBadge({ state }: { state: Today["state"] }) {
  const tone = state === "clocked_in" ? "success" : state === "on_break" ? "warning" : "neutral";
  return <Badge tone={tone}>{STATE_LABELS[state]}</Badge>;
}

function Timeline({ today }: { today: Today }) {
  return (
    <ol aria-label="Today's events" className="flex flex-col gap-2 border-l-2 border-neutral-200 pl-4">
      {today.day.events.map((event) => (
        <li key={event.id} className="relative text-sm">
          <span aria-hidden className="absolute -left-[1.3rem] top-1.5 size-2 rounded-full bg-accent-600" />
          <span className="tabular font-semibold text-neutral-900">
            {formatTime(event.occurred_at, today.time_zone)}
          </span>{" "}
          <span className="text-neutral-700">{eventLabel(event.event_type)}</span>
        </li>
      ))}
    </ol>
  );
}
