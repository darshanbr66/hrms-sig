import type { ReactNode } from "react";

/** A bordered surface for one group of related content, with an optional heading and action.
 * Panels are never nested (ui-ux-guidelines.md §3). */
export function Panel({
  title,
  description,
  action,
  children,
  className = "",
  padded = true,
}: {
  title?: string | undefined;
  description?: string | undefined;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  const headingId = title ? `panel-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}` : undefined;
  return (
    <section
      aria-labelledby={headingId}
      className={`flex flex-col rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 ${className}`}
    >
      {title ? (
        <header className="flex flex-wrap items-start justify-between gap-2 border-b border-neutral-200 px-4 py-3">
          <div className="min-w-0">
            <h2 id={headingId} className="text-base font-semibold text-neutral-900">
              {title}
            </h2>
            {description ? <p className="mt-0.5 text-sm text-neutral-600">{description}</p> : null}
          </div>
          {action}
        </header>
      ) : null}
      <div className={`flex-1 ${padded ? "p-4" : ""}`}>{children}</div>
    </section>
  );
}
