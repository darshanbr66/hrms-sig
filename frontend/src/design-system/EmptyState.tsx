import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/** One sentence saying what would appear here and, if the person can act, the action. */
export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon?: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-[var(--radius-surface)] border border-dashed border-neutral-300 bg-neutral-0 px-5 py-6">
      {Icon ? <Icon aria-hidden className="size-5 text-neutral-500" /> : null}
      <p className="font-semibold text-neutral-900">{title}</p>
      {children ? <div className="max-w-prose text-sm text-neutral-600">{children}</div> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
