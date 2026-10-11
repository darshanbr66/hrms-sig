/**
 * Leave types and their effective-dated policies (`leave.policy.manage`, product-requirements
 * LV-1 and §6.12). Nothing is pre-filled: every rule is a choice HR makes, so the forms start
 * empty and require each one. A policy is never edited: a new version takes over from its date.
 * Holders of `leave.balance.adjust` run the upfront allocation of a leave year here.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarCog, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, localIsoDate } from "../../lib/format";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { EMPLOYMENT_TYPES } from "../employees/queries";
import { options, useLocations } from "../organization/queries";
import { LEAVE_KEY, formatDays, useLeaveTypes, type LeavePolicy, type LeaveType } from "./queries";

type Allocation = components["schemas"]["AllocationResult"];
const YES_NO = [
  { id: "yes", label: "Yes" },
  { id: "no", label: "No" },
];
const MONTHS = Array.from({ length: 12 }, (_, index) => ({
  id: String(index + 1),
  label: new Date(2001, index, 1).toLocaleDateString(undefined, { month: "long" }),
}));
const RULES: {
  key: keyof Pick<
    LeaveType,
    "is_paid" | "requires_approval" | "allows_half_day" | "counts_holidays" | "counts_weekly_offs"
  >;
  label: string;
  help: string;
}[] = [
  { key: "is_paid", label: "Paid leave", help: "Whether this leave is paid. Payroll uses it later." },
  {
    key: "requires_approval",
    label: "Needs approval",
    help: "If not, leave is recorded as soon as the employee asks.",
  },
  {
    key: "allows_half_day",
    label: "Half days allowed",
    help: "Whether a request can start in the afternoon or end at midday.",
  },
  {
    key: "counts_holidays",
    label: "Holidays count as leave",
    help: "Whether a holiday inside a request uses a day of leave.",
  },
  {
    key: "counts_weekly_offs",
    label: "Weekly offs count as leave",
    help: "Whether a weekly off inside a request uses a day. Weekly offs arrive with shifts; until then no day is a weekly off.",
  },
];

function yesNo(value: boolean | undefined): string | null {
  return value === undefined ? null : value ? "yes" : "no";
}

export function LeaveTypesPage() {
  const me = useMe();
  const canManage = hasPermission(me.data, "leave.policy.manage");
  const canAllocate = hasPermission(me.data, "leave.balance.adjust");
  const [showArchived, setShowArchived] = useState(false);
  const types = useLeaveTypes(showArchived);
  const policies = useQuery({
    queryKey: [...LEAVE_KEY, "policies"],
    queryFn: () => unwrap(client.GET("/api/v1/leave-policies")),
  });
  const [editing, setEditing] = useState<LeaveType | "new" | null>(null);
  const [versioning, setVersioning] = useState<LeaveType | null>(null);
  const [allocating, setAllocating] = useState<LeaveType | null>(null);
  const items = types.data?.items ?? [];
  return (
    <div>
      <PageHeader
        title="Leave types"
        description="Sigvitas's leave types and the policies that say who gets them, how much and from when. Nothing here is assumed: HR enters each rule."
        action={
          canManage ? (
            <Button
              onPress={() => {
                setEditing("new");
              }}
            >
              <Plus aria-hidden className="size-4" />
              Add leave type
            </Button>
          ) : undefined
        }
      />
      <div className="mb-4">
        <Checkbox isSelected={showArchived} onChange={setShowArchived}>
          Show archived leave types
        </Checkbox>
      </div>
      {types.isPending ? <Skeleton label="Loading leave types" lines={4} /> : null}
      {types.isError ? (
        <ProblemMessage
          error={types.error}
          fallback="We couldn't load the leave types."
          onRetry={() => void types.refetch()}
        />
      ) : null}
      {types.isSuccess && items.length === 0 ? (
        <EmptyState icon={CalendarCog} title="No leave types yet">
          Add each leave type Sigvitas offers, then a policy saying who it covers and how many days they get.
        </EmptyState>
      ) : null}
      <div className="flex flex-col gap-6">
        {items.map((leaveType) => (
          <TypePanel
            key={leaveType.id}
            leaveType={leaveType}
            policies={(policies.data?.items ?? []).filter((p) => p.leave_type.id === leaveType.id)}
            canManage={canManage}
            canAllocate={canAllocate}
            onEdit={() => {
              setEditing(leaveType);
            }}
            onVersion={() => {
              setVersioning(leaveType);
            }}
            onAllocate={() => {
              setAllocating(leaveType);
            }}
          />
        ))}
      </div>
      {editing ? (
        <TypeDialog
          leaveType={editing === "new" ? null : editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
      {versioning ? (
        <PolicyDialog
          leaveType={versioning}
          onClose={() => {
            setVersioning(null);
          }}
        />
      ) : null}
      {allocating ? (
        <AllocationDialog
          leaveType={allocating}
          onClose={() => {
            setAllocating(null);
          }}
        />
      ) : null}
    </div>
  );
}

function TypePanel({
  leaveType,
  policies,
  canManage,
  canAllocate,
  onEdit,
  onVersion,
  onAllocate,
}: {
  leaveType: LeaveType;
  policies: LeavePolicy[];
  canManage: boolean;
  canAllocate: boolean;
  onEdit: () => void;
  onVersion: () => void;
  onAllocate: () => void;
}) {
  const columns: Column<LeavePolicy>[] = [
    {
      key: "scope",
      header: "Covers",
      primary: true,
      cell: (row) =>
        [
          row.location?.name ?? "Every location",
          row.employment_type
            ? EMPLOYMENT_TYPES.find((t) => t.id === row.employment_type)?.label
            : "every employment type",
        ]
          .filter(Boolean)
          .join(", "),
    },
    {
      key: "dates",
      header: "In effect",
      cell: (row) =>
        `${formatDate(row.effective_from, { weekday: false })} to ${row.effective_to ? formatDate(row.effective_to, { weekday: false }) : "now"}`,
    },
    {
      key: "year",
      header: "Leave year starts",
      cell: (row) => `${row.leave_year_start_day} ${MONTHS[row.leave_year_start_month - 1]?.label ?? ""}`,
    },
    {
      key: "allocation",
      header: "Allocation",
      cell: (row) => (row.accrual_amount ? `${formatDays(row.accrual_amount)} each leave year` : "None"),
    },
    {
      key: "negative",
      header: "Below zero",
      cell: (row) =>
        row.allow_negative_balance && row.max_negative_days
          ? `Up to ${formatDays(row.max_negative_days)}`
          : "Not allowed",
    },
  ];
  const rules = RULES.filter((rule) => leaveType[rule.key]).map((rule) => rule.label);
  return (
    <Panel
      title={leaveType.name}
      description={leaveType.description ?? undefined}
      action={
        <div className="flex flex-wrap gap-2">
          {leaveType.status === "archived" ? <Badge tone="neutral">Archived</Badge> : null}
          {canAllocate && leaveType.status === "active" && policies.some((p) => p.accrual_amount) ? (
            <Button size="sm" variant="secondary" onPress={onAllocate}>
              Allocate a leave year
            </Button>
          ) : null}
          {canManage && leaveType.status === "active" ? (
            <Button size="sm" variant="secondary" onPress={onVersion}>
              Add policy version
            </Button>
          ) : null}
          {canManage ? (
            <Button size="sm" variant="quiet" onPress={onEdit}>
              Edit<span className="sr-only"> {leaveType.name}</span>
            </Button>
          ) : null}
        </div>
      }
    >
      <p className="mb-3 text-sm text-neutral-700">
        <code className="font-mono text-xs">{leaveType.code}</code> ·{" "}
        {rules.length > 0 ? rules.join(", ") : "Unpaid, no approval, whole days only"}
      </p>
      {policies.length === 0 ? (
        <Banner tone="warning">No policy yet, so nobody can request this leave. Add a policy version.</Banner>
      ) : (
        <DataTable
          label={`Policies for ${leaveType.name}`}
          columns={columns}
          rows={policies}
          rowKey={(row) => row.id}
        />
      )}
    </Panel>
  );
}

function useSave<T>(save: () => Promise<T>, done: string, onDone: () => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: save,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
      toast(done);
      onDone();
    },
  });
}

function SaveError({ error }: { error: unknown }) {
  if (!error || Object.keys(fieldMessages(error)).length > 0) return null;
  return <ProblemMessage error={error} fallback="We couldn't save. Try again." />;
}

function TypeDialog({ leaveType, onClose }: { leaveType: LeaveType | null; onClose: () => void }) {
  const [code, setCode] = useState(leaveType?.code ?? "");
  const [name, setName] = useState(leaveType?.name ?? "");
  const [description, setDescription] = useState(leaveType?.description ?? "");
  const [rules, setRules] = useState<Record<string, string | null>>(
    Object.fromEntries(RULES.map((rule) => [rule.key, yesNo(leaveType?.[rule.key])])),
  );
  const [status, setStatus] = useState<string>(leaveType?.status ?? "active");
  const [local, setLocal] = useState<Record<string, string>>({});
  const values = Object.fromEntries(RULES.map((rule) => [rule.key, rules[rule.key] === "yes"])) as Record<
    (typeof RULES)[number]["key"],
    boolean
  >;
  const save = useSave(
    () =>
      leaveType
        ? unwrap(
            client.PATCH("/api/v1/leave-types/{leave_type_id}", {
              params: {
                path: { leave_type_id: leaveType.id },
                header: { "If-Match": ifMatch(leaveType.version) },
              },
              body: {
                name,
                description: description.trim() === "" ? null : description.trim(),
                ...values,
                status: status as "active" | "archived",
              },
            }),
          )
        : unwrap(
            client.POST("/api/v1/leave-types", {
              body: {
                code,
                name,
                description: description.trim() === "" ? null : description.trim(),
                ...values,
              },
            }),
          ),
    leaveType ? "Leave type updated" : "Leave type added",
    onClose,
  );
  const errors = { ...fieldMessages(save.error), ...local };
  return (
    <Dialog
      title={leaveType ? `Edit ${leaveType.name}` : "Add leave type"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          for (const rule of RULES) if (rules[rule.key] === null) problems[rule.key] = "Choose yes or no.";
          if (!leaveType && code.trim() === "") problems.code = "Enter a short code.";
          if (name.trim() === "") problems.name = "Enter a name.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        <div className="grid gap-4 sm:grid-cols-2">
          {leaveType ? null : (
            <TextField
              label="Code"
              name="code"
              value={code}
              onChange={setCode}
              maxLength={32}
              description="A short unique reference. It cannot change later."
              error={errors.code}
            />
          )}
          <TextField
            label="Name"
            name="name"
            value={name}
            onChange={setName}
            maxLength={200}
            error={errors.name}
          />
        </div>
        <TextArea
          label="Description (optional)"
          name="description"
          value={description}
          onChange={setDescription}
          maxLength={1000}
          error={errors.description}
        />
        <div className="grid gap-4 sm:grid-cols-2">
          {RULES.map((rule) => (
            <Select
              key={rule.key}
              label={rule.label}
              name={rule.key}
              placeholder="Choose"
              options={YES_NO}
              value={rules[rule.key] ?? null}
              onChange={(next) => {
                setRules((current) => ({ ...current, [rule.key]: next }));
              }}
              description={rule.help}
              error={errors[rule.key]}
            />
          ))}
        </div>
        {leaveType ? (
          <Select
            label="Status"
            name="status"
            options={[
              { id: "active", label: "Active" },
              { id: "archived", label: "Archived (no new requests)" },
            ]}
            value={status}
            onChange={setStatus}
          />
        ) : null}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            {leaveType ? "Save changes" : "Add leave type"}
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

function PolicyDialog({ leaveType, onClose }: { leaveType: LeaveType; onClose: () => void }) {
  const locations = useLocations();
  const [location, setLocation] = useState("all");
  const [employmentType, setEmploymentType] = useState("all");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [month, setMonth] = useState<string | null>(null);
  const [day, setDay] = useState("");
  const [allocation, setAllocation] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [negative, setNegative] = useState<string | null>(null);
  const [maxNegative, setMaxNegative] = useState("");
  const [local, setLocal] = useState<Record<string, string>>({});
  const save = useSave(
    () =>
      unwrap(
        client.POST("/api/v1/leave-policies", {
          body: {
            leave_type_id: leaveType.id,
            location_id: location === "all" ? null : location,
            employment_type: employmentType === "all" ? null : (employmentType as "full_time"),
            effective_from: effectiveFrom,
            leave_year_start_month: Number(month),
            leave_year_start_day: Number(day),
            accrual_method: allocation === "upfront" ? "upfront_per_leave_year" : "none",
            accrual_amount: allocation === "upfront" ? amount.trim() : null,
            allow_negative_balance: negative === "yes",
            max_negative_days: negative === "yes" ? maxNegative.trim() : null,
          },
        }),
      ),
    "Policy version added",
    onClose,
  );
  const errors = { ...fieldMessages(save.error), ...local };
  const halfDays = (value: string) => {
    const number = Number(value);
    return value.trim() !== "" && Number.isFinite(number) && (number * 2) % 1 === 0;
  };
  return (
    <Dialog title={`Add a policy version for ${leaveType.name}`} isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          if (!effectiveFrom) problems.effective_from = "Choose the date it takes effect.";
          if (!month) problems.leave_year_start_month = "Choose the month.";
          const dayNumber = Number(day);
          if (!Number.isInteger(dayNumber) || dayNumber < 1 || dayNumber > 31)
            problems.leave_year_start_day = "Enter a day from 1 to 31.";
          if (allocation === null) problems.accrual_method = "Choose whether days are given each leave year.";
          if (allocation === "upfront" && (!halfDays(amount) || Number(amount) <= 0))
            problems.accrual_amount = "Enter whole or half days above zero.";
          if (negative === null) problems.allow_negative_balance = "Choose yes or no.";
          if (negative === "yes" && (!halfDays(maxNegative) || Number(maxNegative) < 0))
            problems.max_negative_days = "Enter whole or half days, zero or more.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        <p className="text-sm text-neutral-700">
          A new version applies from its date. The version that covers the same people now ends that day; its
          history stays.
        </p>
        <SaveError error={save.error} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Select
            label="Location"
            name="location_id"
            options={[{ id: "all", label: "Every location" }, ...options(locations.data?.items)]}
            value={location}
            onChange={setLocation}
            description="A policy for one location wins over one for every location."
            error={errors.location_id}
          />
          <Select
            label="Employment type"
            name="employment_type"
            options={[{ id: "all", label: "Every employment type" }, ...EMPLOYMENT_TYPES]}
            value={employmentType}
            onChange={setEmploymentType}
          />
          <TextField
            label="In effect from"
            name="effective_from"
            type="date"
            value={effectiveFrom}
            onChange={setEffectiveFrom}
            error={errors.effective_from}
          />
          <div className="grid grid-cols-2 gap-3">
            <Select
              label="Leave year starts (month)"
              name="leave_year_start_month"
              placeholder="Month"
              options={MONTHS}
              value={month}
              onChange={setMonth}
              error={errors.leave_year_start_month}
            />
            <TextField
              label="Day"
              name="leave_year_start_day"
              value={day}
              onChange={setDay}
              inputMode="numeric"
              error={errors.leave_year_start_day}
            />
          </div>
          <Select
            label="Days given each leave year"
            name="accrual_method"
            placeholder="Choose"
            options={[
              { id: "upfront", label: "Yes, at the start of the leave year" },
              { id: "none", label: "No allocation" },
            ]}
            value={allocation}
            onChange={setAllocation}
            description="HR runs the allocation. Proration, monthly accrual and carry-forward are not supported yet."
            error={errors.accrual_method}
          />
          {allocation === "upfront" ? (
            <TextField
              label="Days each leave year"
              name="accrual_amount"
              value={amount}
              onChange={setAmount}
              inputMode="decimal"
              error={errors.accrual_amount}
            />
          ) : (
            <div />
          )}
          <Select
            label="Balance may go below zero"
            name="allow_negative_balance"
            placeholder="Choose"
            options={YES_NO}
            value={negative}
            onChange={setNegative}
            error={errors.allow_negative_balance}
          />
          {negative === "yes" ? (
            <TextField
              label="How far below zero (days)"
              name="max_negative_days"
              value={maxNegative}
              onChange={setMaxNegative}
              inputMode="decimal"
              error={errors.max_negative_days}
            />
          ) : null}
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Add policy version
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

function AllocationDialog({ leaveType, onClose }: { leaveType: LeaveType; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [asOf, setAsOf] = useState(localIsoDate());
  const [result, setResult] = useState<Allocation | null>(null);
  const run = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/leave-allocations", { body: { leave_type_id: leaveType.id, as_of: asOf } }),
      ),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
      setResult(data);
    },
  });
  return (
    <Dialog title={`Allocate ${leaveType.name}`} isOpen onOpenChange={onClose}>
      {result ? (
        <div className="flex flex-col gap-3 text-sm">
          <p className="font-semibold text-neutral-900">Allocation done</p>
          <ul aria-label="Allocation result" className="flex flex-col gap-1 text-neutral-800">
            <li>Allocated now: {result.allocated}</li>
            <li>Already allocated for this leave year: {result.already_allocated}</li>
            <li>No policy covers them: {result.no_policy}</li>
            <li>Their policy gives no allocation: {result.no_allocation_in_policy}</li>
            {result.skipped_own ? (
              <li>Your own record, left for another HR person: {result.skipped_own}</li>
            ) : null}
          </ul>
          <div className="flex justify-end">
            <Button onPress={onClose}>Close</Button>
          </div>
        </div>
      ) : (
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!run.isPending) run.mutate();
          }}
        >
          <p className="text-sm text-neutral-700">
            Every employee employed on the chosen date, whose policy gives days each leave year, receives the
            full amount for the leave year that date falls in. Running it again for the same year changes
            nobody twice. Days are not prorated for people who joined during the year.
          </p>
          {run.isError ? (
            <ProblemMessage error={run.error} fallback="We couldn't run the allocation." />
          ) : null}
          <TextField
            label="A date in the leave year"
            name="as_of"
            type="date"
            value={asOf}
            onChange={setAsOf}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onPress={onClose}>
              Cancel
            </Button>
            <Button type="submit" isPending={run.isPending}>
              Allocate
            </Button>
          </div>
        </Form>
      )}
    </Dialog>
  );
}
