/**
 * Employees employed today per department, designation or location, as the API counts them
 * within the viewer's scope. Each row links to the employee list filtered to that unit. The
 * bar is the row's share of the largest count, drawn from the same figure the row shows.
 */
import { Link } from "react-router";

import type { components } from "../../lib/api-schema";
import { plural } from "../../lib/format";

type Headcount = components["schemas"]["Headcount"];

export function Headcounts({
  items,
  filter,
  limit,
  label,
}: {
  items: readonly Headcount[];
  /** The employee list's URL filter for this kind of unit. */
  filter: "department" | "designation" | "location";
  limit?: number;
  label: string;
}) {
  const shown = limit ? items.slice(0, limit) : items;
  const largest = Math.max(1, ...items.map((item) => item.employed));
  if (shown.length === 0) {
    return <p className="text-sm text-neutral-600">No one is employed here today.</p>;
  }
  return (
    <ul aria-label={label} className="flex flex-col gap-3">
      {shown.map((item) => (
        <li key={item.id} className="text-sm">
          <div className="flex items-baseline justify-between gap-3">
            <Link
              to={`/employees?${filter}=${item.id}`}
              className="min-w-0 truncate font-semibold text-neutral-900 hover:underline"
            >
              {item.name}
            </Link>
            <span className="tabular shrink-0 text-neutral-700">
              {plural(item.employed, "person", "people")}
            </span>
          </div>
          <div aria-hidden className="mt-1 h-1.5 rounded-full bg-neutral-100">
            <div
              className="h-1.5 rounded-full bg-accent-600"
              style={{ width: `${Math.round((item.employed / largest) * 100)}%` }}
            />
          </div>
        </li>
      ))}
      {limit && items.length > limit ? (
        <li className="text-xs text-neutral-600">and {plural(items.length - limit, "more", "more")}</li>
      ) : null}
    </ul>
  );
}
