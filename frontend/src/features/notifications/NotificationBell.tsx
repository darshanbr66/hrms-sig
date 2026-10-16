/** The header's notification button: the unread count, and a link to the full list. */
import { Bell } from "lucide-react";
import { Link } from "react-router";

import { useUnreadCount } from "./queries";

export function NotificationBell({ enabled }: { enabled: boolean }) {
  const unread = useUnreadCount(enabled);
  const count = unread.data?.unread_count ?? 0;
  const label = count > 0 ? `Notifications, ${count} unread` : "Notifications";
  return (
    <Link
      to="/notifications"
      aria-label={label}
      title={label}
      className="relative rounded-[var(--radius-control)] p-2 text-neutral-700 hover:bg-neutral-100"
    >
      <Bell aria-hidden className="size-5" />
      {count > 0 ? (
        <span
          aria-hidden
          className="tabular absolute -right-0.5 -top-0.5 min-w-5 rounded-full bg-danger-700 px-1 text-center text-[0.6875rem] font-semibold leading-5 text-neutral-0"
        >
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
}
