import { ChevronRight } from "lucide-react";
import { Fragment, useId, useState, type ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  align?: "start" | "end";
  /** The row's title on small screens, where rows become stacked items. */
  primary?: boolean;
}

/**
 * A table for lists of records (ui-ux-guidelines.md §6, Tables): a header that stays below the
 * top bar while the page scrolls (`overflow-clip`, not `overflow-hidden`, keeps the rounded
 * corners without making the wrapper a scroll container that sticky would stick to), numbers
 * right-aligned, and on small screens each row becomes a stacked item with its labels instead
 * of scrolling sideways. A record opens from a link in its primary cell, so keyboard and
 * screen-reader users reach it the same way.
 *
 * With `details`, each row gets a disclosure button (`aria-expanded`, `aria-controls`) that
 * shows the row's underlying records in a full-width row beneath it. Details are rendered only
 * while open, so a long list stays light; they appear with a short reveal (none with reduced
 * motion). Nothing important lives only in the details: the row keeps its own summary.
 */
export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
  details,
  detailsLabel = (row: T) => rowKey(row),
}: {
  label: string;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  /** The row's underlying records, shown when the row is expanded. */
  details?: (row: T) => ReactNode;
  /** Names the row in its button ("Show details for …"). */
  detailsLabel?: (row: T) => string;
}) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const baseId = useId();
  const toggle = (key: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  const span = columns.length + (details ? 1 : 0);
  return (
    <div className="overflow-clip rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0">
      <table aria-label={label} className="w-full border-collapse text-sm">
        <thead className="sr-only sm:not-sr-only">
          <tr className="border-b border-neutral-200 bg-neutral-50 text-left">
            {details ? (
              <th scope="col" className="sticky top-14 z-10 w-10 bg-neutral-50 px-2 py-2.5">
                <span className="sr-only">Details</span>
              </th>
            ) : null}
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`sticky top-14 z-10 bg-neutral-50 px-4 py-2.5 text-xs font-semibold text-neutral-600 ${
                  column.align === "end" ? "text-right" : ""
                }`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="block sm:table-row-group">
          {rows.map((row) => {
            const key = rowKey(row);
            const expanded = open.has(key);
            const panelId = `${baseId}-${key}`;
            return (
              <Fragment key={key}>
                <tr
                  className={`block px-4 py-3 hover:bg-neutral-50 sm:table-row sm:p-0 ${
                    expanded ? "bg-neutral-50" : "border-b border-neutral-200 last:border-b-0"
                  }`}
                >
                  {details ? (
                    <td className="block pb-1 sm:table-cell sm:w-10 sm:px-2 sm:py-3 sm:align-middle">
                      <button
                        type="button"
                        aria-expanded={expanded}
                        aria-controls={expanded ? panelId : undefined}
                        onClick={() => {
                          toggle(key);
                        }}
                        className="flex items-center gap-1 rounded-[var(--radius-control)] p-1 text-sm text-accent-700 hover:bg-accent-50"
                      >
                        <ChevronRight
                          aria-hidden
                          className={`size-4 transition-transform duration-[var(--duration-fast)] ${
                            expanded ? "rotate-90" : ""
                          }`}
                        />
                        <span className="sm:sr-only">
                          {expanded ? "Hide details" : "Show details"}
                          <span className="sr-only"> for {detailsLabel(row)}</span>
                        </span>
                      </button>
                    </td>
                  ) : null}
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      data-label={column.header}
                      className={`flex justify-between gap-4 py-0.5 sm:table-cell sm:px-4 sm:py-3 sm:align-middle ${
                        column.align === "end" ? "tabular sm:text-right" : ""
                      } ${
                        column.primary
                          ? "font-semibold text-neutral-900"
                          : "text-neutral-700 before:text-xs before:font-semibold before:text-neutral-500 before:content-[attr(data-label)] sm:before:hidden"
                      }`}
                    >
                      {column.cell(row)}
                    </td>
                  ))}
                </tr>
                {details && expanded ? (
                  <tr className="block border-b border-neutral-200 bg-neutral-50 last:border-b-0 sm:table-row">
                    <td
                      id={panelId}
                      colSpan={span}
                      className="block px-4 pb-4 sm:table-cell sm:pl-12 sm:pr-4"
                    >
                      <div className="animate-reveal">{details(row)}</div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
