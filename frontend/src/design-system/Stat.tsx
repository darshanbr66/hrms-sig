import type { ReactNode } from "react";
import { Link } from "react-router";

/** A compact figure with its label. Shown only for real numbers the person can act on. */
export function Stat({
  label,
  value,
  hint,
  to,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  to?: string;
}) {
  const body = (
    <>
      <dt className="text-sm text-neutral-600">{label}</dt>
      <dd className="tabular mt-1 text-[1.75rem] font-semibold leading-none text-neutral-900">{value}</dd>
      {hint ? <dd className="mt-2 text-xs text-neutral-600">{hint}</dd> : null}
    </>
  );
  const className = "block rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 px-4 py-3";
  return to ? (
    <div>
      <Link to={to} className={`${className} transition-colors hover:border-accent-600`}>
        <dl>{body}</dl>
      </Link>
    </div>
  ) : (
    <dl className={className}>{body}</dl>
  );
}

export function StatGrid({
  children,
  label,
  columns = 4,
}: {
  children: ReactNode;
  label: string;
  /** How many figures share a row on wide screens; phones show two, tablets three. */
  columns?: 4 | 5;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={`grid grid-cols-2 gap-3 sm:grid-cols-3 ${columns === 5 ? "lg:grid-cols-5" : "lg:grid-cols-4"}`}
    >
      {children}
    </div>
  );
}
