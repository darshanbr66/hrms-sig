/**
 * Leave balances and the ledger behind them, for the signed-in employee (`employeeId` null) or
 * for one employee as HR or their manager sees them. Every figure comes from the API: the
 * balance is the sum of the ledger, reserved days are pending requests, and available is the
 * balance minus reserved. Holders of `leave.balance.adjust` record adjustments and opening
 * balances here, with a reason; the API refuses one's own balance.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarOff, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { TextArea } from "../../design-system/Fields";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDate, localIsoDate } from "../../lib/format";
import { fieldMessages } from "../../lib/problem";
import {
  ENTRY_LABELS,
  LEAVE_KEY,
  formatDays,
  useBalances,
  useLedger,
  useLeaveTypes,
  type Balance,
  type LedgerEntry,
} from "./queries";

export function BalanceCards({ items }: { items: readonly Balance[] }) {
  return (
    <ul aria-label="Leave balances" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {items.map((item) => (
        <li
          key={item.leave_type.id}
          className="rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 px-4 py-3"
        >
          <div className="flex items-start justify-between gap-2">
            <p className="text-sm font-semibold text-neutral-900">{item.leave_type.name}</p>
            {item.eligible ? null : <Badge tone="neutral">Not available now</Badge>}
          </div>
          <p className="tabular mt-2 text-[1.75rem] font-semibold leading-none text-neutral-900">
            {formatDays(item.available)}
          </p>
          <p className="mt-1 text-xs text-neutral-600">
            available · {formatDays(item.balance)} balance
            {Number(item.reserved) > 0 ? ` · ${formatDays(item.reserved)} waiting for approval` : ""}
          </p>
          <p className="mt-1 text-xs text-neutral-600">
            {item.is_paid ? "Paid" : "Unpaid"}
            {item.allows_half_day ? " · half days allowed" : ""}
            {item.allow_negative_balance && item.max_negative_days
              ? ` · may go ${formatDays(item.max_negative_days)} below zero`
              : ""}
          </p>
        </li>
      ))}
    </ul>
  );
}

export function LeaveBalances({
  employeeId,
  ownLabel = false,
}: {
  employeeId: string | null;
  ownLabel?: boolean;
}) {
  const me = useMe();
  const balances = useBalances(employeeId);
  const ledger = useLedger(employeeId);
  const [adjusting, setAdjusting] = useState(false);
  const canAdjust =
    employeeId !== null &&
    hasPermission(me.data, "leave.balance.adjust") &&
    me.data?.employee_id !== employeeId;
  const items = balances.data?.items ?? [];
  const entries = ledger.data?.items ?? [];
  const columns: Column<LedgerEntry>[] = [
    {
      key: "date",
      header: "Date",
      primary: true,
      cell: (row) => formatDate(row.effective_date, { weekday: false }),
    },
    { key: "type", header: "Leave type", cell: (row) => row.leave_type.name },
    { key: "entry", header: "Entry", cell: (row) => ENTRY_LABELS[row.entry_type] },
    {
      key: "amount",
      header: "Days",
      align: "end",
      cell: (row) => (
        <span className={`tabular ${Number(row.amount) < 0 ? "text-danger-700" : "text-success-700"}`}>
          {Number(row.amount) > 0 ? "+" : ""}
          {formatDays(row.amount)}
        </span>
      ),
    },
    { key: "reason", header: "Reason", cell: (row) => row.reason ?? "" },
  ];
  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="balances-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="balances-heading" className="text-base font-semibold text-neutral-900">
            {ownLabel ? "Your balances" : "Balances"}
          </h2>
          {canAdjust ? (
            <Button
              size="sm"
              variant="secondary"
              onPress={() => {
                setAdjusting(true);
              }}
            >
              <Plus aria-hidden className="size-4" />
              Adjust a balance
            </Button>
          ) : null}
        </div>
        {balances.isPending ? <Skeleton label="Loading balances" lines={3} /> : null}
        {balances.isError ? (
          <ProblemMessage
            error={balances.error}
            fallback="We couldn't load the balances."
            onRetry={() => void balances.refetch()}
          />
        ) : null}
        {balances.isSuccess && items.length === 0 ? (
          <EmptyState icon={CalendarOff} title="No leave balances">
            {ownLabel
              ? "No leave type is set up for you yet. HR adds leave types and the policies that cover you."
              : "No leave policy covers this person yet, and nothing is recorded in their leave ledger."}
          </EmptyState>
        ) : null}
        {items.length > 0 ? <BalanceCards items={items} /> : null}
      </section>

      <Panel
        title="Ledger"
        description="Every change to the balances, newest first. Nothing is ever removed."
        padded={false}
      >
        {ledger.isPending ? (
          <div className="p-4">
            <Skeleton label="Loading the ledger" lines={3} />
          </div>
        ) : null}
        {ledger.isError ? (
          <div className="p-4">
            <ProblemMessage
              error={ledger.error}
              fallback="We couldn't load the ledger."
              onRetry={() => void ledger.refetch()}
            />
          </div>
        ) : null}
        {ledger.isSuccess && entries.length === 0 ? (
          <p className="p-4 text-sm text-neutral-600">No entries yet.</p>
        ) : null}
        {entries.length > 0 ? (
          <DataTable label="Leave ledger" columns={columns} rows={entries} rowKey={(row) => row.id} />
        ) : null}
      </Panel>

      {adjusting && employeeId ? (
        <AdjustDialog
          employeeId={employeeId}
          onClose={() => {
            setAdjusting(false);
          }}
        />
      ) : null}
    </div>
  );
}

function AdjustDialog({ employeeId, onClose }: { employeeId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const types = useLeaveTypes(true);
  const [requestKey] = useState(() => crypto.randomUUID());
  const [leaveType, setLeaveType] = useState<string | null>(null);
  const [entryType, setEntryType] = useState<string>("adjustment");
  const [amount, setAmount] = useState("");
  const [effectiveDate, setEffectiveDate] = useState(localIsoDate());
  const [reason, setReason] = useState("");
  const [local, setLocal] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/employees/{employee_id}/leave-adjustments", {
          params: { path: { employee_id: employeeId }, header: { "Idempotency-Key": requestKey } },
          body: {
            leave_type_id: leaveType ?? "",
            entry_type: entryType as "adjustment" | "opening_balance",
            amount: amount.trim(),
            effective_date: effectiveDate,
            reason: reason.trim(),
          },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
      toast("Balance adjusted");
      onClose();
    },
  });
  const errors = { ...fieldMessages(save.error), ...local };
  const fieldProblem = Object.keys(fieldMessages(save.error)).length > 0;
  return (
    <Dialog title="Adjust a balance" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          if (!leaveType) problems.leave_type_id = "Choose a leave type.";
          const value = Number(amount);
          if (amount.trim() === "" || !Number.isFinite(value) || value === 0 || (value * 2) % 1 !== 0) {
            problems.amount = "Enter whole or half days, such as 2, 0.5 or -1.";
          }
          if (reason.trim() === "") problems.reason = "Say why, for the record.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        {save.error && !fieldProblem ? (
          <ProblemMessage error={save.error} fallback="We couldn't record the change. Try again." />
        ) : null}
        <Select
          label="Leave type"
          name="leave_type_id"
          placeholder="Choose a leave type"
          options={(types.data?.items ?? []).map((item) => ({ id: item.id, label: item.name }))}
          value={leaveType}
          onChange={setLeaveType}
          error={errors.leave_type_id}
        />
        <Select
          label="Kind of entry"
          name="entry_type"
          options={[
            { id: "adjustment", label: "Adjustment" },
            { id: "opening_balance", label: "Opening balance (once per leave year)" },
          ]}
          value={entryType}
          onChange={setEntryType}
        />
        <TextField
          label="Days"
          name="amount"
          value={amount}
          onChange={setAmount}
          inputMode="decimal"
          description="Positive adds days, negative removes them. Whole or half days."
          error={errors.amount}
        />
        <TextField
          label="Effective date"
          name="effective_date"
          type="date"
          value={effectiveDate}
          onChange={setEffectiveDate}
          error={errors.effective_date}
        />
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={500}
          description="Recorded in the ledger and the audit log. The employee sees it."
          error={errors.reason}
        />
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Record the change
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
