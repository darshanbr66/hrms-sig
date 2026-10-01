import type { ReactNode } from "react";

/** A titled section of a page: headings and whitespace, not nested cards. */
export function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="border-t border-neutral-200 py-6" aria-labelledby={idFor(title)}>
      <h2 id={idFor(title)} className="text-[1.125rem] font-semibold text-neutral-900">
        {title}
      </h2>
      {description ? <p className="mt-1 max-w-prose text-sm text-neutral-600">{description}</p> : null}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function idFor(title: string): string {
  return `section-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}
