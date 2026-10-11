/**
 * User accounts (`user.read.all`): search, state at a glance, and inviting a new account
 * (`user.invite`). Each account opens to its detail page for the actions the viewer may take.
 */
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { UserPlus, UserRoundCog } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useNavigate } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDateTime } from "../../lib/format";
import { fieldMessages } from "../../lib/problem";
import { ACCOUNT_STATUS, USERS_KEY, type User } from "./users";

export function UsersPage() {
  const me = useMe();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [inviting, setInviting] = useState(false);
  const users = useInfiniteQuery({
    queryKey: [...USERS_KEY, "list", q],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/users", {
          params: { query: { limit: 50, ...(q ? { q } : {}), ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = users.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<User>[] = [
    {
      key: "email",
      header: "Account",
      primary: true,
      cell: (row) => (
        <Link className="truncate hover:underline" to={`/admin/users/${row.id}`}>
          {row.email}
        </Link>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (row) => (
        <span className="flex flex-wrap gap-1">
          <Badge tone={ACCOUNT_STATUS[row.status].tone}>{ACCOUNT_STATUS[row.status].label}</Badge>
          {row.locked ? <Badge tone="danger">Locked</Badge> : null}
          {row.mfa_reenrolment_required ? <Badge tone="warning">Re-enrolling MFA</Badge> : null}
        </span>
      ),
    },
    {
      key: "employee",
      header: "Employee record",
      cell: (row) => (row.employee_id ? "Linked" : "Account only"),
    },
    {
      key: "login",
      header: "Last sign-in",
      cell: (row) => (row.last_login_at ? formatDateTime(row.last_login_at) : "Never"),
    },
    { key: "created", header: "Created", cell: (row) => formatDateTime(row.created_at) },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Users"
        description="Every sign-in account. Accounts are never deleted: they are disabled, which signs them out everywhere."
        action={
          hasPermission(me.data, "user.invite") ? (
            <Button
              onPress={() => {
                setInviting(true);
              }}
            >
              <UserPlus aria-hidden className="size-4" />
              Invite user
            </Button>
          ) : undefined
        }
      />
      <div className="max-w-md">
        <SearchField
          label="Search accounts"
          placeholder="Search by email"
          value={text}
          onChange={(value) => {
            setText(value);
            if (value === "") setQ("");
          }}
          onSubmit={(value) => {
            setQ(value.trim());
          }}
        />
      </div>
      {users.isPending ? <Skeleton label="Loading accounts" lines={6} /> : null}
      {users.isError ? (
        <ProblemMessage
          error={users.error}
          fallback="We couldn't load the accounts."
          onRetry={() => void users.refetch()}
        />
      ) : null}
      {users.isSuccess && rows.length === 0 ? (
        <EmptyState icon={UserRoundCog} title={q ? "No accounts match this search" : "No accounts yet"} />
      ) : null}
      {rows.length > 0 ? (
        <div>
          <DataTable label="User accounts" columns={columns} rows={rows} rowKey={(row) => row.id} />
          <LoadMore
            shown={rows.length}
            hasMore={users.hasNextPage}
            isLoading={users.isFetchingNextPage}
            onLoad={() => void users.fetchNextPage()}
          />
        </div>
      ) : null}
      {inviting ? (
        <InviteDialog
          onClose={() => {
            setInviting(false);
          }}
        />
      ) : null}
    </div>
  );
}

function InviteDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [email, setEmail] = useState("");
  const invite = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/users", { body: { email: email.trim() } })),
    onSuccess: async (user) => {
      await queryClient.invalidateQueries({ queryKey: USERS_KEY });
      toast(`Invite sent to ${user.email}`);
      void navigate(`/admin/users/${user.id}`);
    },
  });
  const errors = fieldMessages(invite.error);
  return (
    <Dialog title="Invite user" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          invite.mutate();
        }}
      >
        <p className="text-sm text-neutral-700">
          The person receives a single-use link to set a password and an authenticator app. The invite grants
          no role. To link an employee record, invite them from that employee&apos;s page instead.
        </p>
        {invite.isError && !errors.email ? (
          <ProblemMessage error={invite.error} fallback="We couldn't send the invite. Try again." />
        ) : null}
        <TextField
          label="Work email"
          name="email"
          type="email"
          value={email}
          onChange={setEmail}
          error={errors.email}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={invite.isPending}>
            Send invite
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
