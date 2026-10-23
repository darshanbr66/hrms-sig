/**
 * One account (docs/authorization-model.md §3.1, §6): its state, the actions the viewer may
 * take, its sessions and its roles. Consequential actions are confirmed and need step-up; the
 * API enforces every rule, including that nobody acts on their own account here (SOD-3, SOD-4),
 * that administrator accounts are disabled only by a system admin, and that a super admin is
 * assigned only with a second super admin's approval.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, KeyRound, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useParams } from "react-router";

import { useStepUp } from "../../app/StepUp";
import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { ConfirmDialog, Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, DescriptionList, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDateTime } from "../../lib/format";
import { fieldMessages, problemText } from "../../lib/problem";
import { ACCOUNT_STATUS, ENABLE_BLOCKED, USERS_KEY, type User } from "./users";

type Assignment = components["schemas"]["AssignmentView"];
type AdminSession = components["schemas"]["AdminSessionView"];

type Action = "disable" | "enable" | "mfa-reset" | "resend" | "revoke-invite" | "revoke-sessions";

export function UserPage() {
  const { userId = "" } = useParams();
  const me = useMe();
  const user = useQuery({
    queryKey: [...USERS_KEY, "one", userId],
    queryFn: () => unwrap(client.GET("/api/v1/users/{user_id}", { params: { path: { user_id: userId } } })),
  });
  const [action, setAction] = useState<Action | null>(null);

  if (user.isPending) return <Skeleton label="Loading the account" lines={6} />;
  if (user.isError) {
    return (
      <div>
        <BackLink />
        <ProblemMessage
          error={user.error}
          fallback="We couldn't load this account."
          onRetry={() => void user.refetch()}
        />
      </div>
    );
  }
  const account = user.data;
  const own = me.data?.user_id === account.id;
  const can = (permission: string) => hasPermission(me.data, permission) && !own;
  const actions: { id: Action; label: string; variant: "secondary" | "danger" }[] = [];
  if (account.status === "invited" && can("user.invite")) {
    actions.push({ id: "resend", label: "Resend invite", variant: "secondary" });
    actions.push({ id: "revoke-invite", label: "Revoke invite", variant: "danger" });
  }
  if (account.status === "active" && can("security.mfa.reset")) {
    actions.push({ id: "mfa-reset", label: "Reset MFA", variant: "secondary" });
  }
  if (account.status === "active" && can("user.disable")) {
    actions.push({ id: "disable", label: "Disable account", variant: "danger" });
  }
  // Only an ordinarily disabled account can be enabled again; a leaver's or an erased
  // account shows why instead (the API refuses both whoever asks).
  if (account.status === "disabled" && !account.enable_blocked && can("user.disable")) {
    actions.push({ id: "enable", label: "Enable account", variant: "secondary" });
  }
  return (
    <div className="flex flex-col gap-6">
      <BackLink />
      <PageHeader
        title={account.email}
        description="Accounts are disabled rather than deleted, so their history stays. A disabled account can be enabled again unless the person has left or their data was erased."
        action={
          actions.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {actions.map((item) => (
                <Button
                  key={item.id}
                  variant={item.variant}
                  onPress={() => {
                    setAction(item.id);
                  }}
                >
                  {item.label}
                </Button>
              ))}
            </div>
          ) : undefined
        }
      />
      {account.enable_blocked ? (
        <Banner tone="info">{ENABLE_BLOCKED[account.enable_blocked].explanation}</Banner>
      ) : null}
      {own ? (
        <Banner tone="info">
          This is your own account. Change your own password, authenticators and sessions in{" "}
          <Link className="underline" to="/account/security">
            Account security
          </Link>
          .
        </Banner>
      ) : null}
      <Panel title="Account">
        <DescriptionList
          items={[
            {
              label: "Status",
              value: (
                <span className="flex flex-wrap gap-1">
                  <Badge tone={ACCOUNT_STATUS[account.status].tone}>
                    {ACCOUNT_STATUS[account.status].label}
                  </Badge>
                  {account.enable_blocked ? (
                    <Badge tone="neutral">{ENABLE_BLOCKED[account.enable_blocked].badge}</Badge>
                  ) : null}
                  {account.locked ? <Badge tone="danger">Locked after failed sign-ins</Badge> : null}
                  {account.mfa_reenrolment_required ? (
                    <Badge tone="warning">Waiting to set up a new authenticator</Badge>
                  ) : null}
                </span>
              ),
            },
            {
              label: "Employee record",
              value: account.employee_id ? (
                hasPermission(me.data, "employee.profile.read.all") ? (
                  <Link className="text-accent-700 underline" to={`/employees/${account.employee_id}`}>
                    Open the employee record
                  </Link>
                ) : (
                  "Linked"
                )
              ) : (
                "Not linked"
              ),
            },
            {
              label: "Last sign-in",
              value: account.last_login_at ? formatDateTime(account.last_login_at) : "Never",
            },
            { label: "Created", value: formatDateTime(account.created_at) },
          ]}
        />
      </Panel>
      {hasPermission(me.data, "role.read") ? <RolesPanel user={account} own={own} /> : null}
      {hasPermission(me.data, "auth.session.read.all") ? (
        <SessionsPanel
          user={account}
          canRevoke={can("auth.session.revoke.all")}
          onRevoke={() => {
            setAction("revoke-sessions");
          }}
        />
      ) : null}
      {action ? (
        <AccountAction
          user={account}
          action={action}
          onClose={() => {
            setAction(null);
          }}
        />
      ) : null}
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/admin/users"
      className="inline-flex items-center gap-1 text-sm text-accent-700 hover:underline"
    >
      <ArrowLeft aria-hidden className="size-4" />
      Users
    </Link>
  );
}

const ACTION_TEXT: Record<
  Action,
  { title: string; confirm: string; body: string; done: string; tone: "danger" | "primary" }
> = {
  disable: {
    title: "Disable this account?",
    confirm: "Disable account",
    body: "They will be signed out on every device and cannot sign in until the account is enabled again.",
    done: "Account disabled",
    tone: "danger",
  },
  enable: {
    title: "Enable this account?",
    confirm: "Enable account",
    body: "They can sign in again with their password and authenticator app.",
    done: "Account enabled",
    tone: "primary",
  },
  "mfa-reset": {
    title: "Reset this person's MFA?",
    confirm: "Reset MFA",
    body: "Their authenticators, recovery codes, sessions and recognized browsers stop working. We email them a link to set up a new authenticator, which also needs their password. Only do this after checking who they are.",
    done: "MFA reset; a re-enrolment link was emailed",
    tone: "danger",
  },
  resend: {
    title: "Resend the invite?",
    confirm: "Resend invite",
    body: "A new invite link is emailed; earlier links stop working.",
    done: "Invite sent again",
    tone: "primary",
  },
  "revoke-invite": {
    title: "Revoke the invite?",
    confirm: "Revoke invite",
    body: "The invite link stops working and the account is disabled.",
    done: "Invite revoked",
    tone: "danger",
  },
  "revoke-sessions": {
    title: "Sign this person out everywhere?",
    confirm: "Sign out everywhere",
    body: "Every session of this account ends now. They can sign in again with their password and authenticator app.",
    done: "Signed out everywhere",
    tone: "danger",
  },
};

function AccountAction({ user, action, onClose }: { user: User; action: Action; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const [reason, setReason] = useState("");
  const path = { params: { path: { user_id: user.id } } };
  const run = useMutation({
    mutationFn: () =>
      withStepUp(async () => {
        switch (action) {
          case "disable":
            await unwrap(client.POST("/api/v1/users/{user_id}/disable", path));
            return;
          case "enable":
            await unwrap(client.POST("/api/v1/users/{user_id}/enable", path));
            return;
          case "mfa-reset":
            await unwrap(
              client.POST("/api/v1/users/{user_id}/mfa-reset", { ...path, body: { reason: reason.trim() } }),
            );
            return;
          case "resend":
            await unwrap(client.POST("/api/v1/users/{user_id}/invite", path));
            return;
          case "revoke-invite":
            await unwrap(client.DELETE("/api/v1/users/{user_id}/invite", path));
            return;
          case "revoke-sessions":
            await unwrap(client.DELETE("/api/v1/users/{user_id}/sessions", path));
            return;
        }
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: USERS_KEY });
      toast(ACTION_TEXT[action].done);
      onClose();
    },
  });
  const text = ACTION_TEXT[action];
  const errors = fieldMessages(run.error);
  return (
    <ConfirmDialog
      title={text.title}
      isOpen
      onOpenChange={onClose}
      confirmLabel={text.confirm}
      tone={text.tone}
      isPending={run.isPending}
      onConfirm={() => {
        run.mutate();
      }}
    >
      <p className="mb-2 font-semibold text-neutral-900">{user.email}</p>
      <p>{text.body}</p>
      {action === "mfa-reset" ? (
        <div className="mt-4">
          <TextArea
            label="Reason"
            name="reason"
            value={reason}
            onChange={setReason}
            maxLength={500}
            description="Record how you checked who they are. It is kept in the audit log; do not include personal details."
            error={errors.reason}
          />
        </div>
      ) : null}
      {run.isError && !errors.reason ? (
        <p className="mt-3 text-danger-700">
          {problemText(run.error, "We couldn't complete this. Try again.")}
        </p>
      ) : null}
    </ConfirmDialog>
  );
}

function SessionsPanel({
  user,
  canRevoke,
  onRevoke,
}: {
  user: User;
  canRevoke: boolean;
  onRevoke: () => void;
}) {
  const sessions = useQuery({
    queryKey: [...USERS_KEY, "sessions", user.id],
    queryFn: () =>
      unwrap(client.GET("/api/v1/users/{user_id}/sessions", { params: { path: { user_id: user.id } } })),
  });
  const items = sessions.data?.items ?? [];
  const columns: Column<AdminSession>[] = [
    { key: "device", header: "Browser", primary: true, cell: (row) => row.user_agent ?? "Unknown browser" },
    { key: "ip", header: "Address", cell: (row) => row.ip ?? "Unknown" },
    { key: "started", header: "Started", cell: (row) => formatDateTime(row.created_at) },
    { key: "active", header: "Last active", cell: (row) => formatDateTime(row.last_activity_at) },
  ];
  return (
    <Panel
      title="Active sessions"
      description="Opening this list is recorded in the audit log."
      action={
        canRevoke && items.length > 0 ? (
          <Button variant="secondary" size="sm" onPress={onRevoke}>
            Sign out everywhere
          </Button>
        ) : undefined
      }
    >
      {sessions.isPending ? <Skeleton label="Loading sessions" lines={2} /> : null}
      {sessions.isError ? (
        <ProblemMessage
          error={sessions.error}
          fallback="We couldn't load the sessions."
          onRetry={() => void sessions.refetch()}
        />
      ) : null}
      {sessions.isSuccess && items.length === 0 ? (
        <p className="text-sm text-neutral-600">No active sessions.</p>
      ) : null}
      {items.length > 0 ? (
        <DataTable label="Active sessions" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
    </Panel>
  );
}

function RolesPanel({ user, own }: { user: User; own: boolean }) {
  const me = useMe();
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const key = [...USERS_KEY, "roles", user.id];
  const [assigning, setAssigning] = useState(false);
  const [removing, setRemoving] = useState<Assignment | null>(null);
  const assignments = useQuery({
    queryKey: key,
    queryFn: () =>
      unwrap(client.GET("/api/v1/users/{user_id}/roles", { params: { path: { user_id: user.id } } })),
  });
  const remove = useMutation({
    mutationFn: (assignment: Assignment) =>
      withStepUp(() =>
        unwrap(
          client.DELETE("/api/v1/users/{user_id}/roles/{assignment_id}", {
            params: { path: { user_id: user.id, assignment_id: assignment.id } },
          }),
        ),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: key });
      toast("Role removed");
      setRemoving(null);
    },
  });
  const canAssign = hasPermission(me.data, "role.assign") && !own;
  const items = assignments.data?.items ?? [];
  const columns: Column<Assignment>[] = [
    { key: "role", header: "Role", primary: true, cell: (row) => row.role_key.replace(/_/g, " ") },
    {
      key: "scope",
      header: "Limited to",
      cell: (row) => (row.department_id || row.location_id ? "A department or location" : "Everyone"),
    },
    { key: "from", header: "From", cell: (row) => formatDateTime(row.valid_from) },
    {
      key: "until",
      header: "Until",
      cell: (row) => (row.valid_until ? formatDateTime(row.valid_until) : "No end"),
    },
    {
      key: "source",
      header: "Granted",
      cell: (row) =>
        row.grant_request_id ? <Badge tone="warning">Through an access request</Badge> : row.grant_reason,
    },
    ...(canAssign
      ? [
          {
            key: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Assignment) =>
              row.grant_request_id ? (
                <Link className="text-sm text-accent-700 underline" to="/admin/access-requests">
                  Manage request
                </Link>
              ) : (
                <Button
                  variant="quiet"
                  size="sm"
                  onPress={() => {
                    setRemoving(row);
                  }}
                >
                  Remove<span className="sr-only"> {row.role_key}</span>
                </Button>
              ),
          },
        ]
      : []),
  ];
  return (
    <Panel
      title="Roles"
      description="Assigned roles only. The employee and manager roles follow from the employee record and reporting lines."
      action={
        canAssign && user.status !== "disabled" ? (
          <Button
            variant="secondary"
            size="sm"
            onPress={() => {
              setAssigning(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add roles
          </Button>
        ) : undefined
      }
    >
      {canAssign && items.some((item) => item.role_key === "super_admin") ? (
        <Banner tone="info">
          To hand over this super admin role, add super admin for the successor and have another super admin
          approve it, then remove the role here. At least two active super admins must remain, and nobody
          removes their own role. A super admin who needs HR, payroll or system access asks for it on the
          access requests page, and another super admin approves it for a limited time.
        </Banner>
      ) : null}
      {assignments.isPending ? <Skeleton label="Loading roles" lines={2} /> : null}
      {assignments.isError ? (
        <ProblemMessage
          error={assignments.error}
          fallback="We couldn't load the roles."
          onRetry={() => void assignments.refetch()}
        />
      ) : null}
      {assignments.isSuccess && items.length === 0 ? (
        <EmptyState icon={KeyRound} title="No assigned roles">
          Without a role this account reaches only its own security settings, and its own employee data if
          linked.
        </EmptyState>
      ) : null}
      {items.length > 0 ? (
        <DataTable label="Assigned roles" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
      {assigning ? (
        <AssignRolesDialog
          user={user}
          held={items}
          queryKey={key}
          onClose={() => {
            setAssigning(false);
          }}
        />
      ) : null}
      {removing ? (
        <ConfirmDialog
          title={`Remove the ${removing.role_key.replace(/_/g, " ")} role?`}
          isOpen
          onOpenChange={() => {
            setRemoving(null);
          }}
          confirmLabel="Remove role"
          isPending={remove.isPending}
          onConfirm={() => {
            remove.mutate(removing);
          }}
        >
          <p>
            {user.email} loses the permissions of this role at once. The change is recorded in the audit log.
          </p>
          {remove.isError ? (
            <p className="mt-3 text-danger-700">
              {problemText(remove.error, "We couldn't remove the role.")}
            </p>
          ) : null}
        </ConfirmDialog>
      ) : null}
    </Panel>
  );
}

// SOD-10 (docs/authorization-model.md §6): a super admin holds none of these as a standing role.
const DATA_ROLES = new Set(["hr", "hr_admin", "payroll_admin", "system_admin", "auditor"]);

/**
 * Add roles to an account (C4). Roles already held are shown ticked and locked: this dialog
 * only adds, so it never replaces a role. The API decides compatibility; the reasons shown
 * here explain its rules before a request is made. Several roles go in one request, one
 * transaction and one step-up; super admin is assigned on its own, as a request another
 * super admin approves.
 */
function AssignRolesDialog({
  user,
  held,
  queryKey,
  onClose,
}: {
  user: User;
  held: readonly Assignment[];
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const roles = useQuery({
    queryKey: ["access", "roles"],
    queryFn: () => unwrap(client.GET("/api/v1/roles")),
  });
  const [chosen, setChosen] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const heldKeys = new Set(held.map((item) => item.role_key));
  const superAdminChosen = chosen.includes("super_admin");
  const assign = useMutation({
    mutationFn: () =>
      withStepUp(async () => {
        const path = { params: { path: { user_id: user.id } } };
        if (superAdminChosen) {
          const result = await unwrap(
            client.POST("/api/v1/users/{user_id}/roles", {
              ...path,
              body: { role_key: "super_admin", reason: reason.trim() },
            }),
          );
          return "kind" in result ? "requested" : "assigned";
        }
        await unwrap(
          client.POST("/api/v1/users/{user_id}/roles/batch", {
            ...path,
            body: { roles: chosen.map((role_key) => ({ role_key })), reason: reason.trim() },
          }),
        );
        return "assigned";
      }),
    onSuccess: async (outcome) => {
      await queryClient.invalidateQueries({ queryKey });
      await queryClient.invalidateQueries({ queryKey: ["access", "grant-requests"] });
      toast(
        outcome === "requested"
          ? "Request created; another super admin must approve it"
          : chosen.length === 1
            ? "Role assigned"
            : `${String(chosen.length)} roles assigned`,
      );
      onClose();
    },
  });
  const options = (roles.data?.items ?? []).filter((item) => !item.is_derived);
  const unavailable = (key: string): string | null => {
    if (heldKeys.has(key)) return "Already assigned. It stays as it is.";
    if (key === "super_admin") {
      if ([...heldKeys].some((role) => DATA_ROLES.has(role)))
        return "Remove their HR, payroll, system admin and auditor roles first: a super admin holds none of them permanently.";
      if (chosen.some((role) => role !== "super_admin"))
        return "Assigned on its own, because another super admin must approve it. Clear the other roles to choose it.";
      return null;
    }
    if (heldKeys.has("super_admin") && DATA_ROLES.has(key))
      return "Not with super admin. A super admin gets this role only for a limited time, through an access request another super admin approves.";
    if (superAdminChosen) return "Clear super admin to choose other roles.";
    return null;
  };
  const errors = fieldMessages(assign.error);
  return (
    <Dialog title={`Add roles for ${user.email}`} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!assign.isPending) assign.mutate();
        }}
      >
        {assign.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage
            error={assign.error}
            fallback="We couldn't assign the roles. Nothing was changed."
          />
        ) : null}
        {roles.isPending ? <Skeleton label="Loading roles" lines={4} /> : null}
        {roles.isError ? (
          <ProblemMessage
            error={roles.error}
            fallback="We couldn't load the roles."
            onRetry={() => void roles.refetch()}
          />
        ) : null}
        {options.length > 0 ? (
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-1 text-sm font-semibold text-neutral-800">Roles to add</legend>
            {options.map((item) => {
              const why = unavailable(item.key);
              return (
                <div key={item.key}>
                  <Checkbox
                    isSelected={heldKeys.has(item.key) || chosen.includes(item.key)}
                    isDisabled={why !== null}
                    onChange={(selected) => {
                      setChosen((current) =>
                        selected ? [...current, item.key] : current.filter((key) => key !== item.key),
                      );
                    }}
                  >
                    <span className="font-semibold">{item.name}</span>
                    <span className="block text-neutral-600">{item.description}</span>
                  </Checkbox>
                  {why ? <p className="mt-1 ml-6 text-xs text-neutral-700">{why}</p> : null}
                </div>
              );
            })}
          </fieldset>
        ) : null}
        {superAdminChosen ? (
          <Banner tone="warning">
            Assigning super admin creates a request. Another super admin, who is neither you nor this person,
            must approve it.
          </Banner>
        ) : null}
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={500}
          description="Kept with each assignment and in the audit log."
          error={errors.reason}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            isPending={assign.isPending}
            isDisabled={chosen.length === 0 || reason.trim() === ""}
          >
            {superAdminChosen ? "Request super admin" : chosen.length > 1 ? "Add roles" : "Add role"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
