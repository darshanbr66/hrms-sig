/**
 * Permanently deleting a person's data (owner decision 2026-10-23, docs/database-design.md
 * §9.1). Super admins only, and two of them: one asks, a different one approves and runs it.
 * Both type the person's employee code, both step up, and the page says plainly what is deleted,
 * what is anonymized and what is kept, before and after. It never says that everything was
 * deleted: history the law or the system must keep stays under the bare record ID, and the
 * report lists it. Offboarding and disabling an account are separate and come first.
 *
 * Showing an action is cosmetic: the API decides each one, including who may approve.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eraser } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { useStepUp } from "../../app/StepUp";
import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, DescriptionList, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Section } from "../../design-system/Section";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDateTime } from "../../lib/format";
import { fieldMessages, problemText } from "../../lib/problem";

type Erasure = components["schemas"]["ErasureView"];
type Category = components["schemas"]["ErasureCategoryView"];

const KEY = ["erasure", "requests"] as const;

const STATUS: Record<
  Erasure["status"],
  { label: string; tone: "neutral" | "warning" | "success" | "danger" }
> = {
  pending: { label: "Waiting for a second super admin", tone: "warning" },
  rejected: { label: "Rejected", tone: "neutral" },
  cancelled: { label: "Withdrawn", tone: "neutral" },
  files_pending: { label: "Records erased; files still being removed", tone: "warning" },
  completed: { label: "Complete; history kept as listed", tone: "success" },
};

const OUTCOME: Record<
  Category["outcome"],
  { label: string; tone: "neutral" | "warning" | "success" | "danger" }
> = {
  deleted: { label: "Deleted", tone: "success" },
  anonymized: { label: "Anonymized", tone: "success" },
  retained: { label: "Kept", tone: "neutral" },
  pending: { label: "Being removed", tone: "warning" },
};

/** What the approval does, said before anyone asks for it. */
function Consequences() {
  return (
    <div className="flex flex-col gap-2 text-sm text-neutral-800">
      <p>
        <span className="font-semibold">Deleted:</span> personal details, emergency contacts, the password,
        authenticators, recovery codes, sessions and recognized browsers, notifications and emails, and every
        stored document file.
      </p>
      <p>
        <span className="font-semibold">Anonymized:</span> the name, employee code and work contact details on
        the employee record and the account, and reasons and notes typed on leave requests, attendance
        corrections and job history.
      </p>
      <p>
        <span className="font-semibold">Kept under the record ID:</span> attendance events, the leave ledger
        and decisions, job and status history, role history and the audit log. They must not change, and they
        stay until their retention period ends. The result lists each of them.
      </p>
      <p>This cannot be undone, and the account can never be enabled again.</p>
    </div>
  );
}

export function ErasuresPage() {
  const me = useMe();
  const [requesting, setRequesting] = useState(false);
  const [open, setOpen] = useState<Erasure | null>(null);
  const requests = useQuery({
    queryKey: KEY,
    queryFn: () => unwrap(client.GET("/api/v1/employee-erasures", { params: { query: { limit: 100 } } })),
  });
  const canRequest = hasPermission(me.data, "employee.erasure.request");
  const items = requests.data?.items ?? [];
  const columns: Column<Erasure>[] = [
    {
      key: "person",
      header: "Person",
      primary: true,
      cell: (row) =>
        row.employee ? (
          <span>
            <span className="block">{row.employee.name}</span>
            <span className="block text-xs text-neutral-600">{row.employee.employee_code}</span>
          </span>
        ) : (
          "Unknown record"
        ),
    },
    {
      key: "status",
      header: "Status",
      cell: (row) => <Badge tone={STATUS[row.status].tone}>{STATUS[row.status].label}</Badge>,
    },
    { key: "requested", header: "Asked for", cell: (row) => formatDateTime(row.requested_at) },
    { key: "by", header: "By", cell: (row) => row.requested_by.email ?? "Unknown account" },
    {
      key: "open",
      header: "Details",
      cell: (row) => (
        <Button
          size="sm"
          variant="quiet"
          onPress={() => {
            setOpen(row);
          }}
        >
          Open<span className="sr-only"> the request about {row.employee?.name ?? "this record"}</span>
        </Button>
      ),
    },
  ];
  return (
    <div>
      <PageHeader
        title="Data erasure"
        description="Permanently delete an offboarded person's data. One super admin asks, a different super admin approves, and both are recorded in the audit log. Offboard the person first; their account must not hold the super admin or system admin role."
        action={
          canRequest ? (
            <Button
              variant="danger"
              onPress={() => {
                setRequesting(true);
              }}
            >
              <Eraser aria-hidden className="size-4" />
              Permanently delete employee data
            </Button>
          ) : undefined
        }
      />
      <Section title="Requests">
        {requests.isPending ? <Skeleton label="Loading erasure requests" lines={4} /> : null}
        {requests.isError ? (
          <ProblemMessage
            error={requests.error}
            fallback="We couldn't load the erasure requests."
            onRetry={() => void requests.refetch()}
          />
        ) : null}
        {requests.isSuccess && items.length === 0 ? (
          <EmptyState icon={Eraser} title="No erasure requests">
            Requests to delete a person&apos;s data appear here, with what was deleted and what was kept.
          </EmptyState>
        ) : null}
        {items.length > 0 ? (
          <DataTable label="Erasure requests" columns={columns} rows={items} rowKey={(row) => row.id} />
        ) : null}
      </Section>
      {requesting ? (
        <RequestDialog
          onClose={() => {
            setRequesting(false);
          }}
        />
      ) : null}
      {open ? (
        <ErasureDialog
          erasure={items.find((item) => item.id === open.id) ?? open}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
    </div>
  );
}

function RequestDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const [code, setCode] = useState("");
  const [reason, setReason] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [local, setLocal] = useState<string | null>(null);
  const send = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/employee-erasures", {
            body: { employee_code: code.trim(), reason: reason.trim() },
          }),
        ),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: KEY });
      toast("Erasure requested; another super admin must approve it");
      onClose();
    },
  });
  const errors = fieldMessages(send.error);
  return (
    <Dialog title="Permanently delete employee data" isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (code.trim() === "") setLocal("Enter the person's employee code.");
          else if (reason.trim() === "") setLocal("Say why the erasure is authorized and necessary.");
          else if (!understood) setLocal("Confirm that you understand what is deleted and what is kept.");
          else {
            setLocal(null);
            if (!send.isPending) send.mutate();
          }
        }}
      >
        <Banner tone="warning">
          Nothing is deleted yet. A different super admin must approve this request and type the employee code
          again.
        </Banner>
        <Consequences />
        {local ? <Banner tone="danger">{local}</Banner> : null}
        {send.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={send.error} fallback="We couldn't send the request." />
        ) : null}
        <TextField
          label="Employee code"
          name="employee_code"
          value={code}
          onChange={setCode}
          description="As shown on the employee record, which HR can tell you."
          error={errors.employee_code}
        />
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={500}
          description="Who authorized it and why it is necessary. Kept in the audit log: no personal details."
          error={errors.reason}
        />
        <Checkbox isSelected={understood} onChange={setUnderstood}>
          I understand that the approval cannot be undone, and that the history listed above is kept.
        </Checkbox>
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="danger" isPending={send.isPending}>
            Ask for erasure
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function ErasureDialog({ erasure, onClose }: { erasure: Erasure; onClose: () => void }) {
  const me = useMe();
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [local, setLocal] = useState<string | null>(null);
  const own = erasure.requested_by.id === me.data?.user_id;
  const waiting = erasure.status === "pending";
  const canApprove = waiting && !own && hasPermission(me.data, "employee.erasure.approve");
  const path = { params: { path: { erasure_id: erasure.id } } };
  const done = async (message: string) => {
    await queryClient.invalidateQueries({ queryKey: KEY });
    toast(message);
  };
  const approve = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/employee-erasures/{erasure_id}/approve", {
            ...path,
            body: { employee_code: code.trim(), ...(note.trim() ? { note: note.trim() } : {}) },
          }),
        ),
      ),
    onSuccess: async (result) => {
      await done(
        result.status === "completed"
          ? "Erasure complete; the history listed was kept"
          : "Records erased; the stored files are being removed",
      );
    },
  });
  const reject = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/employee-erasures/{erasure_id}/reject", {
            ...path,
            body: { note: note.trim() },
          }),
        ),
      ),
    onSuccess: async () => {
      await done("Erasure request rejected");
      onClose();
    },
  });
  const cancel = useMutation({
    mutationFn: () =>
      withStepUp(() => unwrap(client.POST("/api/v1/employee-erasures/{erasure_id}/cancel", path))),
    onSuccess: async () => {
      await done("Erasure request withdrawn");
      onClose();
    },
  });
  const failure = approve.error ?? reject.error ?? cancel.error;
  const errors = fieldMessages(approve.error);
  return (
    <Dialog
      title={erasure.employee ? `Erasure of ${erasure.employee.name}'s data` : "Erasure request"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <div className="flex flex-col gap-4">
        <DescriptionList
          items={[
            { label: "Person", value: erasure.employee?.name ?? "Unknown record" },
            { label: "Employee code", value: erasure.employee?.employee_code ?? "Unknown" },
            { label: "Status", value: STATUS[erasure.status].label },
            {
              label: "Asked for by",
              value: `${erasure.requested_by.email ?? "Unknown account"}, ${formatDateTime(erasure.requested_at)}`,
            },
            { label: "Reason", value: erasure.reason },
            ...(erasure.decided_by
              ? [
                  {
                    label: erasure.status === "rejected" ? "Rejected by" : "Approved by",
                    value: `${erasure.decided_by.email ?? "Unknown account"}, ${
                      erasure.decided_at ? formatDateTime(erasure.decided_at) : ""
                    }`,
                  },
                ]
              : []),
            ...(erasure.decision_note ? [{ label: "Note", value: erasure.decision_note }] : []),
          ]}
        />
        {erasure.status === "files_pending" ? (
          <Banner tone="warning">
            The records are erased. {erasure.files_remaining} of {erasure.files_total} stored files are still
            being removed, so this request is not complete yet.
            {erasure.last_file_error ? " The last attempt failed; it is tried again every minute." : ""}
          </Banner>
        ) : null}
        {erasure.status === "completed" ? (
          <Banner tone="info">
            Complete. The data listed as deleted or anonymized is gone; the records listed as kept remain
            under the record ID until their retention period ends.
          </Banner>
        ) : null}
        {erasure.result ? <ResultTable categories={erasure.result} /> : null}
        {failure && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={failure} fallback="We couldn't complete this. Nothing was changed." />
        ) : null}
        {local ? <Banner tone="danger">{local}</Banner> : null}
        {canApprove ? (
          <Form
            className="flex flex-col gap-4 border-t border-neutral-200 pt-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (code.trim() === "") setLocal("Type the person's employee code to confirm.");
              else if (!understood) setLocal("Confirm that you understand what is deleted and what is kept.");
              else {
                setLocal(null);
                if (!approve.isPending) approve.mutate();
              }
            }}
          >
            <Consequences />
            <TextField
              label="Type the employee code to confirm"
              name="confirm_code"
              value={code}
              onChange={setCode}
              error={errors.employee_code}
            />
            <TextArea
              label="Note"
              name="note"
              value={note}
              onChange={setNote}
              maxLength={500}
              description="Optional when approving; required to reject. Kept in the audit log: no personal details."
            />
            <Checkbox isSelected={understood} onChange={setUnderstood}>
              I understand that this cannot be undone, and that the history listed above is kept.
            </Checkbox>
            <DialogActions>
              <Button
                variant="secondary"
                isPending={reject.isPending}
                onPress={() => {
                  if (note.trim() === "") setLocal("Add a note to say why it is rejected.");
                  else {
                    setLocal(null);
                    reject.mutate();
                  }
                }}
              >
                Reject
              </Button>
              <Button type="submit" variant="danger" isPending={approve.isPending}>
                Approve and delete data
              </Button>
            </DialogActions>
          </Form>
        ) : (
          <DialogActions>
            {waiting && own ? (
              <Button
                variant="secondary"
                isPending={cancel.isPending}
                onPress={() => {
                  cancel.mutate();
                }}
              >
                Withdraw request
              </Button>
            ) : null}
            <Button variant="secondary" onPress={onClose}>
              Close
            </Button>
          </DialogActions>
        )}
        {waiting && own ? (
          <p className="text-sm text-neutral-600">A different super admin must approve your request.</p>
        ) : null}
        {failure && Object.keys(errors).length > 0 ? (
          <p className="text-sm text-danger-700">{problemText(failure, "Check the employee code.")}</p>
        ) : null}
      </div>
    </Dialog>
  );
}

function ResultTable({ categories }: { categories: Category[] }) {
  const columns: Column<Category>[] = [
    { key: "what", header: "Records", primary: true, cell: (row) => row.note },
    {
      key: "outcome",
      header: "Outcome",
      cell: (row) => <Badge tone={OUTCOME[row.outcome].tone}>{OUTCOME[row.outcome].label}</Badge>,
    },
    { key: "count", header: "How many", align: "end", cell: (row) => row.count },
  ];
  return (
    <DataTable
      label="What happened to each kind of record"
      columns={columns}
      rows={categories}
      rowKey={(row) => row.key}
    />
  );
}
