/**
 * Access requests (docs/authorization-model.md §4.3, ui-ux-guidelines.md "Access requests"):
 * elevation requests, super admin assignments and break-glass uses, with their decisions.
 *
 * Everyone with `role.read` sees the list. Super admins also see the actions their permissions
 * allow: ask for temporary access, decide other people's requests, end temporary access, use
 * break-glass, and review someone else's break-glass use. Every action needs step-up. Showing an
 * action is cosmetic: the API decides each one, including who may decide which request.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";

import { useStepUp } from "../../app/StepUp";
import { ELEVATION_KEY, hasPermission, meQueryKey, useMe } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { Dialog } from "../../design-system/Dialog";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Section } from "../../design-system/Section";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDateTime } from "../../lib/format";
import { problemText } from "../../lib/problem";

type GrantRequest = components["schemas"]["GrantRequestView"];

const KEY = ["access", "grant-requests"] as const;

const DURATIONS = [
  { id: "30", label: "30 minutes" },
  { id: "60", label: "1 hour" },
  { id: "120", label: "2 hours" },
  { id: "240", label: "4 hours" },
  { id: "480", label: "8 hours" },
];
const BREAK_GLASS_DURATIONS = [
  { id: "15", label: "15 minutes" },
  { id: "30", label: "30 minutes" },
  { id: "60", label: "1 hour" },
];

const STATUS_TEXT: Record<GrantRequest["status"], string> = {
  pending: "Waiting for a decision",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired without a decision",
  active: "Active",
  ended: "Ended",
  revoked: "Ended early or withdrawn",
};
const KIND_TEXT: Record<GrantRequest["kind"], string> = {
  elevation: "Temporary access",
  super_admin_assignment: "Super admin assignment",
  break_glass: "Break-glass",
};

export function AccessRequestsPage() {
  const me = useMe();
  const requests = useQuery({
    queryKey: KEY,
    queryFn: () => unwrap(client.GET("/api/v1/role-grant-requests", { params: { query: { limit: 100 } } })),
  });
  const canRequest = hasPermission(me.data, "role.elevation.request");
  const canBreakGlass = hasPermission(me.data, "role.elevation.break_glass");
  const items = requests.data?.items ?? [];
  return (
    <div>
      <PageHeader
        title="Access requests"
        description="Temporary access for super admins, super admin assignments and break-glass uses. Every step is recorded in the audit log and other super admins are told by email."
      />
      {canRequest ? (
        <Section title="Ask for temporary access">
          <ElevationForm />
        </Section>
      ) : null}
      <Section title="Requests">
        {requests.isPending ? <Skeleton label="Loading access requests" /> : null}
        {requests.isError ? (
          <ProblemMessage error={requests.error} fallback="We couldn't load the access requests." />
        ) : null}
        {requests.isSuccess && items.length === 0 ? (
          <p className="text-sm text-neutral-600">There are no access requests yet.</p>
        ) : null}
        <ul className="divide-y divide-neutral-200">
          {items.map((item) => (
            <li key={item.id} className="py-4">
              <RequestRow request={item} />
            </li>
          ))}
        </ul>
      </Section>
      {canBreakGlass ? (
        <Section
          title="Break-glass"
          description="For emergencies only, when no other super admin can approve a request. It gives you the system admin role for at most an hour, without approval. Every super admin and auditor is told at once, and another super admin must review it."
        >
          <BreakGlass />
        </Section>
      ) : null}
    </div>
  );
}

function useRefresh() {
  const queryClient = useQueryClient();
  return async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: KEY }),
      queryClient.invalidateQueries({ queryKey: ELEVATION_KEY }),
      queryClient.invalidateQueries({ queryKey: meQueryKey }),
    ]);
  };
}

function ElevationForm() {
  const withStepUp = useStepUp();
  const refresh = useRefresh();
  const roles = useQuery({
    queryKey: ["access", "roles"],
    queryFn: () => unwrap(client.GET("/api/v1/roles")),
  });
  const [role, setRole] = useState<string | null>(null);
  const [duration, setDuration] = useState("120");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<{ field: "role" | "reason" | null; message: string } | null>(null);
  const options = (roles.data?.items ?? [])
    .filter((item) => !item.is_derived && item.key !== "super_admin")
    .map((item) => ({ id: item.key, label: item.name }));
  const roleName = options.find((option) => option.id === role)?.label;
  const durationName = DURATIONS.find((option) => option.id === duration)?.label ?? "";

  const send = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/role-grant-requests", {
            body: { role_key: role ?? "", reason: reason.trim(), duration_minutes: Number(duration) },
          }),
        ),
      ),
    onSuccess: async () => {
      setReason("");
      setError(null);
      await refresh();
    },
    onError: (failure) => {
      setError({ field: null, message: problemText(failure, "We couldn't send the request. Try again.") });
    },
  });

  const submit = () => {
    if (!role) {
      setError({ field: "role", message: "Choose a role." });
      return;
    }
    if (!reason.trim()) {
      setError({ field: "reason", message: "Say why you need this access." });
      return;
    }
    send.mutate();
  };

  return (
    <Form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {roles.isError ? <ProblemMessage error={roles.error} fallback="We couldn't load the roles." /> : null}
      <Select
        label="Role"
        name="role"
        options={options}
        value={role}
        onChange={setRole}
        error={error?.field === "role" ? error.message : undefined}
      />
      <Select
        label="For how long"
        name="duration"
        options={DURATIONS}
        value={duration}
        onChange={setDuration}
      />
      <TextField
        label="Reason"
        name="reason"
        value={reason}
        onChange={setReason}
        maxLength={500}
        description="Other super admins see this when they decide."
        error={error?.field === "reason" ? error.message : undefined}
      />
      <p className="text-sm text-neutral-700">
        {roleName
          ? `Other super admins will be notified. If one of them approves, you get the ${roleName} role for ${durationName}, and it ends automatically.`
          : "Other super admins will be notified. If one of them approves, the access ends automatically."}
      </p>
      {error?.field === null ? <Banner tone="danger">{error.message}</Banner> : null}
      {send.isSuccess ? <Banner tone="success">Your request was sent.</Banner> : null}
      <Button type="submit" isPending={send.isPending}>
        Send request
      </Button>
    </Form>
  );
}

type Action = "approve" | "reject" | "revoke" | "acknowledge";

const ACTION_TEXT: Record<Action, { button: string; title: string; done: string }> = {
  approve: { button: "Approve", title: "Approve this request?", done: "Approve" },
  reject: { button: "Reject", title: "Reject this request?", done: "Reject" },
  revoke: { button: "End now", title: "End this access now?", done: "End now" },
  acknowledge: {
    button: "Mark as reviewed",
    title: "Mark this break-glass use as reviewed?",
    done: "Mark as reviewed",
  },
};

function RequestRow({ request }: { request: GrantRequest }) {
  const me = useMe();
  const userId = me.data?.user_id;
  const canDecide = hasPermission(me.data, "role.elevation.approve");
  const involved = userId === request.subject.id || userId === request.requested_by.id;
  const actions: Action[] = [];
  if (canDecide && request.status === "pending" && !involved) actions.push("approve", "reject");
  if (canDecide && (request.status === "active" || (request.status === "pending" && involved))) {
    actions.push("revoke");
  }
  if (
    canDecide &&
    request.kind === "break_glass" &&
    request.status !== "active" &&
    request.acknowledged_at === null &&
    userId !== request.requested_by.id
  ) {
    actions.push("acknowledge");
  }
  const who = request.subject.email ?? "An account";
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-neutral-900">
        {KIND_TEXT[request.kind]}: {request.role_key} for {who}
      </p>
      <p className="text-sm text-neutral-700">{STATUS_TEXT[request.status]}</p>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm text-neutral-700 sm:grid-cols-[auto_1fr]">
        <dt className="text-neutral-600">Reason</dt>
        <dd className="break-words">{request.reason}</dd>
        <dt className="text-neutral-600">Asked by</dt>
        <dd>
          {request.requested_by.email ?? "An account"}, {formatDateTime(request.requested_at)}
        </dd>
        {request.duration_minutes !== null ? (
          <>
            <dt className="text-neutral-600">Length</dt>
            <dd>{request.duration_minutes} minutes</dd>
          </>
        ) : null}
        {request.decide_by ? (
          <>
            <dt className="text-neutral-600">Decide by</dt>
            <dd>{formatDateTime(request.decide_by)}</dd>
          </>
        ) : null}
        {request.decided_by ? (
          <>
            <dt className="text-neutral-600">Decided by</dt>
            <dd>
              {request.decided_by.email ?? "An account"}
              {request.decision_note ? ` (${request.decision_note})` : ""}
            </dd>
          </>
        ) : null}
        {request.ends_at ? (
          <>
            <dt className="text-neutral-600">{request.status === "active" ? "Ends" : "Was due to end"}</dt>
            <dd>{formatDateTime(request.ends_at)}</dd>
          </>
        ) : null}
        {request.kind === "break_glass" ? (
          <>
            <dt className="text-neutral-600">Review</dt>
            <dd>
              {request.acknowledged_by
                ? `Reviewed by ${request.acknowledged_by.email ?? "an account"}`
                : "Not reviewed yet"}
            </dd>
          </>
        ) : null}
      </dl>
      {actions.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {actions.map((action) => (
            <ActionButton key={action} action={action} request={request} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ActionButton({ action, request }: { action: Action; request: GrantRequest }) {
  const withStepUp = useStepUp();
  const refresh = useRefresh();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const text = ACTION_TEXT[action];
  const run = useMutation({
    mutationFn: () =>
      withStepUp(() => {
        const params = { path: { request_id: request.id } };
        const body = { note: note.trim() || null };
        switch (action) {
          case "approve":
            return unwrap(client.POST("/api/v1/role-grant-requests/{request_id}/approve", { params, body }));
          case "reject":
            return unwrap(client.POST("/api/v1/role-grant-requests/{request_id}/reject", { params, body }));
          case "revoke":
            return unwrap(client.POST("/api/v1/role-grant-requests/{request_id}/revoke", { params }));
          case "acknowledge":
            return unwrap(client.POST("/api/v1/role-grant-requests/{request_id}/acknowledge", { params }));
        }
      }),
    onSuccess: async () => {
      setOpen(false);
      await refresh();
    },
  });
  return (
    <>
      <Button
        variant={action === "approve" || action === "acknowledge" ? "secondary" : "quiet"}
        onPress={() => {
          run.reset();
          setOpen(true);
        }}
      >
        {text.button}
      </Button>
      <Dialog title={text.title} isOpen={open} onOpenChange={setOpen}>
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            run.mutate();
          }}
        >
          <p className="text-sm text-neutral-700">
            {action === "approve"
              ? `${request.subject.email ?? "This account"} gets the ${request.role_key} role${request.duration_minutes ? ` for ${request.duration_minutes} minutes` : ""}. Other super admins will be notified.`
              : action === "revoke"
                ? "The access ends now, or the request is withdrawn. Other super admins will be notified."
                : action === "acknowledge"
                  ? "Confirm that you looked at why break-glass was used and what was done with it."
                  : "Nothing is given. Other super admins will be notified."}
          </p>
          {action === "approve" || action === "reject" ? (
            <TextField label="Note (optional)" name="note" value={note} onChange={setNote} maxLength={500} />
          ) : null}
          {run.isError ? (
            <Banner tone="danger">{problemText(run.error, "That didn't work. Try again.")}</Banner>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onPress={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant={action === "approve" || action === "acknowledge" ? "primary" : "danger"}
              isPending={run.isPending}
            >
              {text.done}
            </Button>
          </div>
        </Form>
      </Dialog>
    </>
  );
}

function BreakGlass() {
  const withStepUp = useStepUp();
  const refresh = useRefresh();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [duration, setDuration] = useState("60");
  const [error, setError] = useState<string | undefined>();
  const use = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/role-grant-requests/break-glass", {
            body: { reason: reason.trim(), duration_minutes: Number(duration) },
          }),
        ),
      ),
    onSuccess: async () => {
      setOpen(false);
      setReason("");
      await refresh();
    },
  });
  return (
    <>
      <Button
        variant="danger"
        onPress={() => {
          use.reset();
          setError(undefined);
          setOpen(true);
        }}
      >
        Use break-glass
      </Button>
      <Dialog title="Use break-glass?" isOpen={open} onOpenChange={setOpen}>
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!reason.trim()) {
              setError("Say why no other super admin can approve a request.");
              return;
            }
            use.mutate();
          }}
        >
          <p className="text-sm text-neutral-700">
            You get the system admin role without approval. Every super admin and auditor is told at once.
          </p>
          <TextField
            label="Reason"
            name="break-glass-reason"
            value={reason}
            onChange={setReason}
            maxLength={500}
            error={error}
          />
          <Select
            label="For how long"
            name="break-glass-duration"
            options={BREAK_GLASS_DURATIONS}
            value={duration}
            onChange={setDuration}
          />
          {use.isError ? (
            <Banner tone="danger">{problemText(use.error, "That didn't work. Try again.")}</Banner>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onPress={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" variant="danger" isPending={use.isPending}>
              Use break-glass
            </Button>
          </div>
        </Form>
      </Dialog>
    </>
  );
}
