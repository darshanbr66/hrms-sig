/** Sections of an employee's record: details, job history, status and the HRMS account. */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Mail, Pencil, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link } from "react-router";
import { z } from "zod";

import { useStepUp } from "../../app/StepUp";
import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { ConfirmDialog, Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, DescriptionList, TextArea } from "../../design-system/Fields";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { applyFieldErrors } from "../../lib/forms";
import { formatDate, formatDateTime, humanize, localIsoDate } from "../../lib/format";
import { fieldMessages, ifMatch, problemText } from "../../lib/problem";
import { ACCOUNT_STATUS } from "../admin/users";
import { JobFields } from "./JobFields";
import {
  PEOPLE_KEY,
  STATUS_LABELS,
  employmentTypeLabel,
  type Employee,
  type EmploymentType,
} from "./queries";

type Job = components["schemas"]["JobView"];
type StatusChange = components["schemas"]["StatusChangeView"];

function useRefreshPeople() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: PEOPLE_KEY });
}

// --- overview -----------------------------------------------------------------------------------

export function OverviewSection({ employee }: { employee: Employee }) {
  const me = useMe();
  const [editing, setEditing] = useState(false);
  const canEdit = hasPermission(me.data, "employee.profile.update.all");
  const job = employee.job;
  return (
    <Panel
      title="Work details"
      action={
        canEdit ? (
          <Button
            variant="secondary"
            size="sm"
            onPress={() => {
              setEditing(true);
            }}
          >
            <Pencil aria-hidden className="size-3.5" />
            Edit details
          </Button>
        ) : undefined
      }
    >
      <DescriptionList
        items={[
          {
            label: "Legal name",
            value: `${employee.legal_first_name} ${employee.legal_last_name ?? ""}`.trim(),
          },
          { label: "Preferred name", value: employee.preferred_name },
          { label: "Work email", value: employee.work_email },
          { label: "Work phone", value: employee.work_phone },
          { label: "Date of joining", value: formatDate(employee.date_of_joining) },
          {
            label: "Last working day",
            value: employee.date_of_exit ? formatDate(employee.date_of_exit) : null,
          },
          { label: "Employment type", value: job ? employmentTypeLabel(job.employment_type) : null },
          { label: "Current job since", value: job ? formatDate(job.effective_from) : null },
        ]}
      />
      {editing ? (
        <EditDetailsDialog
          employee={employee}
          onClose={() => {
            setEditing(false);
          }}
        />
      ) : null}
    </Panel>
  );
}

function EditDetailsDialog({ employee, onClose }: { employee: Employee; onClose: () => void }) {
  const refresh = useRefreshPeople();
  const toast = useToast();
  const [values, setValues] = useState({
    employee_code: employee.employee_code,
    legal_first_name: employee.legal_first_name,
    legal_last_name: employee.legal_last_name ?? "",
    preferred_name: employee.preferred_name ?? "",
    work_email: employee.work_email ?? "",
    work_phone: employee.work_phone ?? "",
  });
  const blankToNull = (value: string) => (value.trim() === "" ? null : value.trim());
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH("/api/v1/employees/{employee_id}", {
          params: { path: { employee_id: employee.id }, header: { "If-Match": ifMatch(employee.version) } },
          body: {
            employee_code: values.employee_code.trim(),
            legal_first_name: values.legal_first_name.trim(),
            legal_last_name: blankToNull(values.legal_last_name),
            preferred_name: blankToNull(values.preferred_name),
            work_email: blankToNull(values.work_email),
            work_phone: blankToNull(values.work_phone),
          },
        }),
      ),
    onSuccess: async () => {
      await refresh();
      toast("Details saved");
      onClose();
    },
  });
  const errors = fieldMessages(save.error);
  const field = (name: keyof typeof values, label: string, type: "text" | "email" | "tel" = "text") => (
    <TextField
      label={label}
      name={name}
      type={type}
      value={values[name]}
      onChange={(value) => {
        setValues((current) => ({ ...current, [name]: value }));
      }}
      error={errors[name]}
    />
  );
  return (
    <Dialog title={`Edit ${employee.name}`} isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't save the details. Try again." />
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          {field("employee_code", "Employee code")}
          {field("legal_first_name", "Legal first name")}
          {field("legal_last_name", "Legal last name (optional)")}
          {field("preferred_name", "Preferred name (optional)")}
          {field("work_email", "Work email (optional)", "email")}
          {field("work_phone", "Work phone (optional)", "tel")}
        </div>
        <p className="text-xs text-neutral-600">
          Job, reporting line and status change through their own sections, so their history is kept.
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Save changes
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

// --- job history --------------------------------------------------------------------------------

const REASONS = [
  { id: "transfer", label: "Transfer" },
  { id: "promotion", label: "Promotion" },
  { id: "manager_change", label: "Manager change" },
  { id: "correction", label: "Correction" },
  { id: "other", label: "Other" },
];

export function JobHistorySection({ employee }: { employee: Employee }) {
  const me = useMe();
  const [changing, setChanging] = useState(false);
  const canChange = hasPermission(me.data, "employee.lifecycle.manage") && employee.status !== "exited";
  const jobs = useQuery({
    queryKey: [...PEOPLE_KEY, "jobs", employee.id],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees/{employee_id}/jobs", {
          params: { path: { employee_id: employee.id } },
        }),
      ),
  });
  const columns: Column<Job>[] = [
    {
      key: "period",
      header: "Period",
      primary: true,
      cell: (job) =>
        `${formatDate(job.effective_from, { weekday: false })} to ${
          job.effective_to ? formatDate(job.effective_to, { weekday: false }) + " (exclusive)" : "now"
        }`,
    },
    { key: "designation", header: "Designation", cell: (job) => job.designation.name },
    { key: "department", header: "Department", cell: (job) => job.department.name },
    { key: "location", header: "Location", cell: (job) => job.location.name },
    { key: "manager", header: "Manager", cell: (job) => job.manager?.name ?? "None" },
    { key: "type", header: "Type", cell: (job) => employmentTypeLabel(job.employment_type) },
    {
      key: "reason",
      header: "Reason",
      cell: (job) => (
        <span>
          {humanize(job.change_reason)}
          {job.notes ? <span className="block text-xs text-neutral-600">{job.notes}</span> : null}
        </span>
      ),
    },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-neutral-600">
          Every change starts a new period; earlier periods are kept as history.
        </p>
        {canChange ? (
          <Button
            size="sm"
            onPress={() => {
              setChanging(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Record a job change
          </Button>
        ) : null}
      </div>
      {jobs.isPending ? <Skeleton label="Loading job history" lines={3} /> : null}
      {jobs.isError ? (
        <ProblemMessage
          error={jobs.error}
          fallback="We couldn't load the job history."
          onRetry={() => void jobs.refetch()}
        />
      ) : null}
      {jobs.isSuccess && jobs.data.items.length === 0 ? (
        <EmptyState icon={History} title="No job history">
          This record has no job periods.
        </EmptyState>
      ) : null}
      {jobs.isSuccess && jobs.data.items.length > 0 ? (
        <DataTable label="Job history" columns={columns} rows={jobs.data.items} rowKey={(job) => job.id} />
      ) : null}
      {changing ? (
        <JobChangeDialog
          employee={employee}
          onClose={() => {
            setChanging(false);
          }}
        />
      ) : null}
    </div>
  );
}

const jobChangeSchema = z.object({
  effective_from: z.string().min(1, "Choose the date the change takes effect."),
  department_id: z.string().min(1, "Choose a department."),
  designation_id: z.string().min(1, "Choose a designation."),
  location_id: z.string().min(1, "Choose a location."),
  manager_employee_id: z.string().nullable(),
  employment_type: z.string().min(1, "Choose the employment type."),
  change_reason: z.string().min(1, "Choose the reason."),
  notes: z
    .string()
    .trim()
    .max(1000, "Use at most 1000 characters.")
    .transform((value) => (value === "" ? null : value)),
});
type JobChangeInput = z.input<typeof jobChangeSchema>;
type JobChangeValues = z.output<typeof jobChangeSchema>;

function JobChangeDialog({ employee, onClose }: { employee: Employee; onClose: () => void }) {
  const refresh = useRefreshPeople();
  const toast = useToast();
  const job = employee.job;
  const form = useForm<JobChangeInput, unknown, JobChangeValues>({
    resolver: zodResolver(jobChangeSchema),
    defaultValues: {
      effective_from: "",
      department_id: job?.department.id ?? "",
      designation_id: job?.designation.id ?? "",
      location_id: job?.location.id ?? "",
      manager_employee_id: job?.manager?.id ?? null,
      employment_type: job?.employment_type ?? "",
      change_reason: "",
      notes: "",
    },
  });
  const submit = form.handleSubmit(async (values) => {
    try {
      await unwrap(
        client.POST("/api/v1/employees/{employee_id}/jobs", {
          params: { path: { employee_id: employee.id } },
          body: {
            ...values,
            employment_type: values.employment_type as EmploymentType,
            change_reason: values.change_reason as "transfer",
          },
        }),
      );
      await refresh();
      toast("Job change recorded");
      onClose();
    } catch (error) {
      const placed = applyFieldErrors(error, form.setError, [
        "effective_from",
        "department_id",
        "designation_id",
        "location_id",
        "manager_employee_id",
        "employment_type",
        "change_reason",
        "notes",
      ]);
      if (!placed)
        form.setError("root", { message: problemText(error, "We couldn't record the change. Try again.") });
    }
  });
  return (
    <Dialog title={`Record a job change for ${employee.name}`} isOpen onOpenChange={onClose} size="lg">
      <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
        {form.formState.errors.root?.message ? (
          <Banner tone="danger">{form.formState.errors.root.message}</Banner>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Controller
            control={form.control}
            name="effective_from"
            render={({ field, fieldState }) => (
              <TextField
                label="Takes effect on"
                name={field.name}
                type="date"
                value={field.value}
                onChange={field.onChange}
                error={fieldState.error?.message}
              />
            )}
          />
          <Controller
            control={form.control}
            name="change_reason"
            render={({ field, fieldState }) => (
              <Select
                label="Reason"
                name={field.name}
                options={REASONS}
                value={field.value || null}
                onChange={field.onChange}
                placeholder="Choose a reason"
                error={fieldState.error?.message}
              />
            )}
          />
        </div>
        <JobFields
          control={form.control}
          prefix=""
          initialManager={job?.manager ?? null}
          excludeEmployeeId={employee.id}
        />
        <Controller
          control={form.control}
          name="notes"
          render={({ field, fieldState }) => (
            <TextArea
              label="Notes (optional)"
              name={field.name}
              value={field.value}
              onChange={field.onChange}
              maxLength={1000}
              description="Seen by HR and the employee, not by managers."
              error={fieldState.error?.message}
            />
          )}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Record change
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

// --- status -------------------------------------------------------------------------------------

const NEXT_STATUSES: Record<Employee["status"], { id: "active" | "on_notice" | "exited"; label: string }[]> =
  {
    pre_joining: [
      { id: "active", label: "Active (has joined)" },
      { id: "exited", label: "Left (did not join)" },
    ],
    active: [
      { id: "on_notice", label: "On notice" },
      { id: "exited", label: "Left" },
    ],
    on_notice: [
      { id: "active", label: "Active (notice withdrawn)" },
      { id: "exited", label: "Left" },
    ],
    exited: [],
  };

export function StatusSection({ employee }: { employee: Employee }) {
  const me = useMe();
  const [changing, setChanging] = useState(false);
  const canChange =
    hasPermission(me.data, "employee.lifecycle.manage") && NEXT_STATUSES[employee.status].length > 0;
  const history = useQuery({
    queryKey: [...PEOPLE_KEY, "status-changes", employee.id],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees/{employee_id}/status-changes", {
          params: { path: { employee_id: employee.id } },
        }),
      ),
  });
  const columns: Column<StatusChange>[] = [
    {
      key: "change",
      header: "Change",
      primary: true,
      cell: (item) => `${STATUS_LABELS[item.from_status]} to ${STATUS_LABELS[item.to_status]}`,
    },
    {
      key: "effective",
      header: "Effective",
      cell: (item) => formatDate(item.effective_date, { weekday: false }),
    },
    { key: "reason", header: "Reason", cell: (item) => item.reason },
    { key: "recorded", header: "Recorded", cell: (item) => formatDateTime(item.created_at) },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-neutral-600">
          Current status: <strong>{STATUS_LABELS[employee.status]}</strong>
        </p>
        {canChange ? (
          <Button
            size="sm"
            onPress={() => {
              setChanging(true);
            }}
          >
            Change status
          </Button>
        ) : null}
      </div>
      {history.isPending ? <Skeleton label="Loading status history" lines={2} /> : null}
      {history.isError ? (
        <ProblemMessage
          error={history.error}
          fallback="We couldn't load the status history."
          onRetry={() => void history.refetch()}
        />
      ) : null}
      {history.isSuccess && history.data.items.length === 0 ? (
        <p className="text-sm text-neutral-600">No status changes recorded since the record was created.</p>
      ) : null}
      {history.isSuccess && history.data.items.length > 0 ? (
        <DataTable
          label="Status history"
          columns={columns}
          rows={history.data.items}
          rowKey={(item) => item.id}
        />
      ) : null}
      {changing ? (
        <StatusDialog
          employee={employee}
          onClose={() => {
            setChanging(false);
          }}
        />
      ) : null}
    </div>
  );
}

function StatusDialog({ employee, onClose }: { employee: Employee; onClose: () => void }) {
  const me = useMe();
  const refresh = useRefreshPeople();
  const toast = useToast();
  const withStepUp = useStepUp();
  const choices = NEXT_STATUSES[employee.status];
  const [status, setStatus] = useState<string>(choices[0]?.id ?? "");
  const [date, setDate] = useState(localIsoDate());
  const [reason, setReason] = useState("");
  const [disableAccount, setDisableAccount] = useState(true);
  const canDisable = hasPermission(me.data, "user.disable") && hasPermission(me.data, "user.read.all");
  const account = useQuery({
    queryKey: [...PEOPLE_KEY, "account", employee.id],
    queryFn: () =>
      unwrap(client.GET("/api/v1/users", { params: { query: { employee_id: employee.id, limit: 1 } } })),
    enabled: canDisable,
  });
  const linked = account.data?.items[0];
  const offerDisable = status === "exited" && canDisable && linked?.status === "active";
  const save = useMutation({
    mutationFn: async () => {
      await unwrap(
        client.POST("/api/v1/employees/{employee_id}/status-changes", {
          params: { path: { employee_id: employee.id } },
          body: { to_status: status as "exited", effective_date: date, reason: reason.trim() },
        }),
      );
      if (offerDisable && disableAccount) {
        await withStepUp(() =>
          unwrap(
            client.POST("/api/v1/users/{user_id}/disable", { params: { path: { user_id: linked.id } } }),
          ),
        );
        return "Status changed and HRMS account disabled";
      }
      return "Status changed";
    },
    onSuccess: async (message) => {
      await refresh();
      toast(message);
      onClose();
    },
  });
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={`Change status of ${employee.name}`} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't change the status. Try again." />
        ) : null}
        <Select label="New status" name="to_status" options={choices} value={status} onChange={setStatus} />
        <TextField
          label={status === "exited" ? "Last working day" : "Effective date"}
          name="effective_date"
          type="date"
          value={date}
          onChange={setDate}
          error={errors.effective_date ?? errors.to_status}
        />
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={500}
          description="Recorded in the status history and the audit log."
          error={errors.reason}
        />
        {offerDisable ? (
          <Checkbox isSelected={disableAccount} onChange={setDisableAccount}>
            Also disable their HRMS account now and sign them out everywhere. You will be asked to confirm
            it&apos;s you.
          </Checkbox>
        ) : null}
        {status === "exited" && linked?.status === "active" && !canDisable ? (
          <Banner tone="warning">
            Their HRMS account stays active until someone with account access disables it.
          </Banner>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending} isDisabled={reason.trim() === ""}>
            Change status
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

// --- HRMS account -------------------------------------------------------------------------------

export function AccountSection({ employee }: { employee: Employee }) {
  const me = useMe();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const accountKey = [...PEOPLE_KEY, "account", employee.id];
  const account = useQuery({
    queryKey: accountKey,
    queryFn: () =>
      unwrap(client.GET("/api/v1/users", { params: { query: { employee_id: employee.id, limit: 1 } } })),
  });
  const invite = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/users", {
          body: { email: employee.work_email ?? "", employee_id: employee.id },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: accountKey });
      setConfirming(false);
      toast("Invite sent");
    },
  });
  const linked = account.data?.items[0];
  const canInvite = hasPermission(me.data, "user.invite");
  const canLink = canInvite && hasPermission(me.data, "employee.lifecycle.manage");
  const [linking, setLinking] = useState(false);
  return (
    <Panel title="HRMS account" description="The sign-in account linked to this record, if any.">
      {account.isPending ? <Skeleton label="Loading the account" lines={2} /> : null}
      {account.isError ? (
        <ProblemMessage
          error={account.error}
          fallback="We couldn't load the account."
          onRetry={() => void account.refetch()}
        />
      ) : null}
      {account.isSuccess && linked ? (
        <div className="flex flex-col gap-3">
          <DescriptionList
            items={[
              { label: "Email", value: linked.email },
              {
                label: "Status",
                value: (
                  <Badge tone={ACCOUNT_STATUS[linked.status].tone}>
                    {ACCOUNT_STATUS[linked.status].label}
                  </Badge>
                ),
              },
              {
                label: "Last sign-in",
                value: linked.last_login_at ? formatDateTime(linked.last_login_at) : "Never",
              },
            ]}
          />
          <Link className="text-sm text-accent-700 underline" to={`/admin/users/${linked.id}`}>
            Manage this account
          </Link>
        </div>
      ) : null}
      {account.isSuccess && !linked ? (
        <EmptyState
          icon={Mail}
          title="No HRMS account yet"
          action={
            employee.work_email && employee.status !== "exited" && canInvite ? (
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onPress={() => {
                    setConfirming(true);
                  }}
                >
                  Invite to the HRMS
                </Button>
                {canLink ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onPress={() => {
                      setLinking(true);
                    }}
                  >
                    Link an existing account
                  </Button>
                ) : null}
              </div>
            ) : undefined
          }
        >
          {employee.work_email
            ? "An invite emails a single-use link. The person sets a password and an authenticator app before they can sign in."
            : "Add a work email to this record before inviting the person."}
        </EmptyState>
      ) : null}
      {linking && employee.work_email ? (
        <LinkAccountDialog
          employee={employee}
          workEmail={employee.work_email}
          onClose={() => {
            setLinking(false);
          }}
          onLinked={async () => {
            await queryClient.invalidateQueries({ queryKey: accountKey });
            setLinking(false);
            toast("Account linked");
          }}
        />
      ) : null}
      <ConfirmDialog
        title={`Invite ${employee.name}?`}
        isOpen={confirming}
        onOpenChange={setConfirming}
        confirmLabel="Send invite"
        tone="primary"
        isPending={invite.isPending}
        onConfirm={() => {
          invite.mutate();
        }}
      >
        <p>
          We will email an invite link to {employee.work_email}. It works once and expires after the
          configured time.
        </p>
        {invite.isError ? (
          <p className="mt-3 text-danger-700">{problemText(invite.error, "We couldn't send the invite.")}</p>
        ) : null}
      </ConfirmDialog>
    </Panel>
  );
}

/**
 * Link an account that already exists (for example, invited before this record existed) to
 * this employee. The API requires the account's email to be the work email, so the dialog
 * looks that email up and says plainly when no account uses it, when it belongs to another
 * record, or when it is an administrator account. Step-up and a reason are required.
 */
function LinkAccountDialog({
  employee,
  workEmail,
  onClose,
  onLinked,
}: {
  employee: Employee;
  workEmail: string;
  onClose: () => void;
  onLinked: () => Promise<void>;
}) {
  const withStepUp = useStepUp();
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | undefined>();
  const candidates = useQuery({
    queryKey: [...PEOPLE_KEY, "account-candidates", workEmail],
    queryFn: () => unwrap(client.GET("/api/v1/users", { params: { query: { q: workEmail, limit: 20 } } })),
  });
  const match = candidates.data?.items.find((item) => item.email.toLowerCase() === workEmail.toLowerCase());
  const link = useMutation({
    mutationFn: (userId: string) =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/users/{user_id}/employee-link", {
            params: { path: { user_id: userId } },
            body: { employee_id: employee.id, reason: reason.trim() },
          }),
        ),
      ),
    onSuccess: onLinked,
  });
  const linkedElsewhere = match?.employee_id && match.employee_id !== employee.id;
  return (
    <Dialog title={`Link an account to ${employee.name}`} isOpen onOpenChange={onClose}>
      <div className="flex flex-col gap-4 text-sm">
        <p className="text-neutral-700">
          The account must use the work email on this record,{" "}
          <span className="font-semibold">{workEmail}</span>. Linking gives it {employee.name}&apos;s
          self-service access.
        </p>
        {candidates.isPending ? <Skeleton label="Looking for the account" lines={2} /> : null}
        {candidates.isError ? (
          <ProblemMessage
            error={candidates.error}
            fallback="We couldn't look up the accounts."
            onRetry={() => void candidates.refetch()}
          />
        ) : null}
        {candidates.isSuccess && !match ? (
          <Banner tone="info">No account uses {workEmail}. Invite the person instead.</Banner>
        ) : null}
        {match && linkedElsewhere ? (
          <Banner tone="warning">
            The account {match.email} is already linked to another employee record. An account belongs to one
            person.
          </Banner>
        ) : null}
        {match && !linkedElsewhere ? (
          <>
            <DescriptionList
              items={[
                { label: "Account", value: match.email },
                {
                  label: "Status",
                  value: (
                    <Badge tone={ACCOUNT_STATUS[match.status].tone}>
                      {ACCOUNT_STATUS[match.status].label}
                    </Badge>
                  ),
                },
              ]}
            />
            <TextArea
              label="Reason"
              name="reason"
              value={reason}
              onChange={setReason}
              maxLength={500}
              description="Recorded in the audit log, for example why the account was not linked when it was invited."
              error={reasonError}
            />
            {link.isError ? (
              <p className="text-danger-700">{problemText(link.error, "We couldn't link the account.")}</p>
            ) : null}
          </>
        ) : null}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          {match && !linkedElsewhere ? (
            <Button
              isPending={link.isPending}
              onPress={() => {
                if (reason.trim() === "") {
                  setReasonError("Say why the account is linked now.");
                  return;
                }
                setReasonError(undefined);
                if (!link.isPending) link.mutate(match.id);
              }}
            >
              Link account
            </Button>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
