import type { ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  align?: "start" | "end";
  /** The row's title on small screens, where rows become stacked items. */
  primary?: boolean;
}

/**
 * A table for lists of records (ui-ux-guidelines.md §6, Tables): sticky header, numbers
 * right-aligned, and on small screens each row becomes a stacked item with its labels instead
 * of scrolling sideways. A record opens from a link in its primary cell, so keyboard and
 * screen-reader users reach it the same way.
 */
export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
}: {
  label: string;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
}) {
  return (
    <div className="overflow-hidden rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0">
      <table aria-label={label} className="w-full border-collapse text-sm">
        <thead className="sr-only sm:not-sr-only">
          <tr className="border-b border-neutral-200 bg-neutral-50 text-left">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`sticky top-0 px-4 py-2.5 text-xs font-semibold text-neutral-600 ${
                  column.align === "end" ? "text-right" : ""
                }`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="block sm:table-row-group">
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className="block border-b border-neutral-200 px-4 py-3 last:border-b-0 hover:bg-neutral-50 sm:table-row sm:p-0"
            >
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
          ))}
        </tbody>
      </table>
    </div>
  );
}
