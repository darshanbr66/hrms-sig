/**
 * System roles and their permissions (docs/authorization-model.md §4). Roles are defined in
 * code and changed only by a reviewed data migration, so this page is read-only. Assigning a
 * role happens on the account's page.
 */
import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";

import { Badge } from "../../design-system/Badge";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import { plural } from "../../lib/format";

export function RolesPage() {
  const roles = useQuery({
    queryKey: ["access", "roles"],
    queryFn: () => unwrap(client.GET("/api/v1/roles")),
  });
  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <PageHeader
        title="Roles"
        description="What each role allows. Employee and manager are derived from the employee record and reporting lines; the others are assigned on an account's page. Super admins have no access to employee data except through an approved, time-limited request."
      />
      {roles.isPending ? <Skeleton label="Loading roles" lines={6} /> : null}
      {roles.isError ? (
        <ProblemMessage
          error={roles.error}
          fallback="We couldn't load the roles."
          onRetry={() => void roles.refetch()}
        />
      ) : null}
      <ul className="flex flex-col gap-3">
        {(roles.data?.items ?? []).map((role) => (
          <li
            key={role.id}
            className="rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0"
          >
            <details className="group">
              <summary className="flex cursor-pointer list-none items-start justify-between gap-4 px-4 py-3">
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-neutral-900">{role.name}</span>
                    {role.is_derived ? <Badge tone="info">Derived</Badge> : <Badge>Assigned</Badge>}
                  </span>
                  <span className="mt-0.5 block text-sm text-neutral-600">{role.description}</span>
                </span>
                <span className="flex shrink-0 items-center gap-2 text-sm text-neutral-600">
                  {plural(role.permissions.length, "permission", "permissions")}
                  <ChevronDown aria-hidden className="size-4 transition-transform group-open:rotate-180" />
                </span>
              </summary>
              <ul className="grid gap-1 border-t border-neutral-200 px-4 py-3 sm:grid-cols-2">
                {[...role.permissions].sort().map((permission) => (
                  <li key={permission}>
                    <code className="font-mono text-xs text-neutral-800">{permission}</code>
                  </li>
                ))}
              </ul>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}
