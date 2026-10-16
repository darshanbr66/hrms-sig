/**
 * Departments as a hierarchy (an expandable table tree, after the Atlassian pattern): each
 * department sits under the one it is part of, with connectors showing the nesting. Only top
 * level departments render at first; a department's children render when it is expanded, so
 * a large organization does not draw every node at once. Choosing a department shows its
 * details. The flat, searchable list stays available beside it, and the people in each
 * department are reached through the employee list, which applies the reader's scope.
 */
import { ChevronRight, Pencil } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { plural } from "../../lib/format";

export interface TreeDepartment {
  id: string;
  code: string;
  name: string;
  description: string | null;
  parent_id: string | null;
  head_name: string | null;
  status: "active" | "archived";
}

interface Node<D extends TreeDepartment> {
  department: D;
  children: Node<D>[];
}

/** Builds the tree. A department whose parent is not in the list (archived and hidden, or
 * outside the search) is shown at the top level; a loop, which the API refuses, is cut. */
function buildTree<D extends TreeDepartment>(departments: readonly D[]): Node<D>[] {
  const byId = new Map(departments.map((department) => [department.id, department]));
  const children = new Map<string | null, D[]>();
  for (const department of departments) {
    const parent = department.parent_id && byId.has(department.parent_id) ? department.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), department]);
  }
  const build = (parent: string | null, seen: ReadonlySet<string>): Node<D>[] =>
    (children.get(parent) ?? [])
      .filter((department) => !seen.has(department.id))
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((department) => ({
        department,
        children: build(department.id, new Set([...seen, department.id])),
      }));
  return build(null, new Set());
}

function countAll(nodes: readonly Node<TreeDepartment>[]): number {
  return nodes.reduce((total, node) => total + 1 + countAll(node.children), 0);
}

export function DepartmentTree<D extends TreeDepartment>({
  departments,
  headcounts,
  onEdit,
  canListPeople,
}: {
  departments: readonly D[];
  /** Whether the reader may open the employee list (it applies their scope). */
  canListPeople: boolean;
  /** People employed per department, when the reader may see headcounts. */
  headcounts: ReadonlyMap<string, number> | null;
  onEdit: ((department: D) => void) | null;
}) {
  const roots = buildTree(departments);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const withChildren = departments.filter((department) =>
    departments.some((other) => other.parent_id === department.id),
  );
  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const chosen = departments.find((department) => department.id === selected) ?? null;
  const chosenChildren = chosen ? departments.filter((department) => department.parent_id === chosen.id) : [];

  const rows = (nodes: readonly Node<D>[], depth: number): React.ReactNode =>
    nodes.map((node, index) => {
      const { department } = node;
      const open = expanded.has(department.id);
      const isSelected = selected === department.id;
      const lastSibling = index === nodes.length - 1;
      return (
        <Fragment key={department.id}>
          <tr
            data-depth={depth}
            className={`border-b border-neutral-200 last:border-b-0 ${isSelected ? "bg-accent-50" : "hover:bg-neutral-50"}`}
          >
            <td className="py-1.5 pr-4 pl-2">
              <div className="flex items-center" style={{ paddingLeft: `${depth * 1.5}rem` }}>
                {depth > 0 ? (
                  <span
                    aria-hidden
                    className={`mr-1 -ml-3 h-5 w-3 self-start border-l border-neutral-300 ${lastSibling ? "rounded-bl border-b" : "border-b"}`}
                  />
                ) : null}
                {node.children.length > 0 ? (
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => {
                      toggle(department.id);
                    }}
                    className="mr-1 rounded-[var(--radius-control)] p-1 text-neutral-700 hover:bg-neutral-100"
                  >
                    <ChevronRight
                      aria-hidden
                      className={`size-4 transition-transform duration-[var(--duration-fast)] ${open ? "rotate-90" : ""}`}
                    />
                    <span className="sr-only">
                      {open ? "Collapse" : "Expand"} {department.name},{" "}
                      {plural(node.children.length, "sub-department", "sub-departments")}
                    </span>
                  </button>
                ) : (
                  <span aria-hidden className="mr-1 inline-block w-6" />
                )}
                <button
                  type="button"
                  aria-pressed={isSelected}
                  onClick={() => {
                    setSelected(isSelected ? null : department.id);
                  }}
                  className="min-w-0 truncate rounded-[var(--radius-control)] px-1 py-0.5 text-left font-semibold text-neutral-900 hover:underline"
                >
                  {depth > 0 ? <span className="sr-only">Level {depth + 1}: </span> : null}
                  {department.name}
                </button>
                {department.status === "archived" ? (
                  <span className="ml-2">
                    <Badge tone="neutral">Archived</Badge>
                  </span>
                ) : null}
              </div>
            </td>
            <td className="hidden px-4 py-1.5 text-neutral-700 sm:table-cell">
              {department.head_name ?? "None"}
            </td>
            {headcounts ? (
              <td className="tabular px-4 py-1.5 text-right text-neutral-700">
                {headcounts.get(department.id) ?? 0}
              </td>
            ) : null}
          </tr>
          {open ? rows(node.children, depth + 1) : null}
        </Fragment>
      );
    });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-neutral-600">
          {plural(countAll(roots), "department", "departments")}, {roots.length} at the top level
        </p>
        {withChildren.length > 0 ? (
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="quiet"
              onPress={() => {
                setExpanded(new Set(withChildren.map((department) => department.id)));
              }}
            >
              Expand all
            </Button>
            <Button
              size="sm"
              variant="quiet"
              onPress={() => {
                setExpanded(new Set());
              }}
            >
              Collapse all
            </Button>
          </div>
        ) : null}
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="overflow-clip rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0">
          <table aria-label="Department hierarchy" className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-xs font-semibold text-neutral-600">
                <th scope="col" className="px-4 py-2.5">
                  Department
                </th>
                <th scope="col" className="hidden px-4 py-2.5 sm:table-cell">
                  Head
                </th>
                {headcounts ? (
                  <th scope="col" className="px-4 py-2.5 text-right">
                    Employees
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody>{rows(roots, 0)}</tbody>
          </table>
        </div>
        <section
          aria-label="Selected department"
          className="self-start rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-4 text-sm"
        >
          {chosen ? (
            <div key={chosen.id} className="animate-reveal flex flex-col gap-3">
              <div>
                <h3 className="text-base font-semibold text-neutral-900">{chosen.name}</h3>
                <code className="font-mono text-xs text-neutral-600">{chosen.code}</code>
              </div>
              {chosen.description ? <p className="text-neutral-700">{chosen.description}</p> : null}
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
                <dt className="text-neutral-600">Part of</dt>
                <dd className="text-neutral-900">
                  {chosen.parent_id
                    ? (departments.find((department) => department.id === chosen.parent_id)?.name ??
                      "Not shown")
                    : "Top level"}
                </dd>
                <dt className="text-neutral-600">Head</dt>
                <dd className="text-neutral-900">{chosen.head_name ?? "None"}</dd>
                <dt className="text-neutral-600">Contains</dt>
                <dd className="text-neutral-900">
                  {chosenChildren.length > 0
                    ? chosenChildren.map((child) => child.name).join(", ")
                    : "No sub-departments"}
                </dd>
                {headcounts ? (
                  <>
                    <dt className="text-neutral-600">Employees</dt>
                    <dd className="tabular text-neutral-900">{headcounts.get(chosen.id) ?? 0}</dd>
                  </>
                ) : null}
              </dl>
              <div className="flex flex-wrap gap-2">
                {canListPeople ? (
                  <Link className="text-accent-700 hover:underline" to={`/employees?department=${chosen.id}`}>
                    People in {chosen.name}
                  </Link>
                ) : null}
                {onEdit ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onPress={() => {
                      onEdit(chosen);
                    }}
                  >
                    <Pencil aria-hidden className="size-3.5" />
                    Edit<span className="sr-only"> {chosen.name}</span>
                  </Button>
                ) : null}
              </div>
            </div>
          ) : (
            <p className="text-neutral-600">Choose a department to see its details.</p>
          )}
        </section>
      </div>
    </div>
  );
}
