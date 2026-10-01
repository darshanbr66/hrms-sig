import type { ReactNode } from "react";

export function PageHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-[1.375rem] font-semibold text-neutral-900">{title}</h1>
        {description ? <p className="mt-1 max-w-prose text-sm text-neutral-600">{description}</p> : null}
      </div>
      {action}
    </header>
  );
}
