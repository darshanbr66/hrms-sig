/**
 * Attendance set-up (product-requirements ATT-9, ATT-11, ATT-12): shifts and their versions,
 * each location's default shift, attendance policies, shift assignments, attendance locks, and
 * the check of who has no shift or policy. Nothing is pre-filled: every time, weekly off and
 * threshold is a value HR enters, and a version that started is never edited.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ComboBox } from "../../design-system/ComboBox";
import { DataTable, type Column } from "../../design-system/DataTable";
import { ConfirmDialog, Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { Stat, StatGrid } from "../../design-system/Stat";
import { Tabs } from "../../design-system/Tabs";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, localIsoDate, plural } from "../../lib/format";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { useEmployeeSearch } from "../employees/queries";
import { options, useLocations } from "../organization/queries";
import { OfficeLocationsTab } from "./OfficeLocationsTab";
import { ATTENDANCE_KEY, WEEKDAYS, describeWeeklyOffs, useShifts, type Policy, type Shift } from "./queries";

type WeeklyOff = components["schemas"]["WeeklyOffValue"];
type Lock = components["schemas"]["LockView"];

function useSave<T>(save: () => Promise<T>, done: string, onDone: () => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: save,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
      toast(done);
      onDone();
    },
  });
}

function SaveError({ error }: { error: unknown }) {
  if (!error || Object.keys(fieldMessages(error)).length > 0) return null;
  return <ProblemMessage error={error} fallback="We couldn't save. Try again." />;
}

function inEffect(from: string, to: string | null | undefined): string {
  return `${formatDate(from, { weekday: false })} to ${to ? formatDate(to, { weekday: false }) : "now"}`;
}

export function AttendanceSetupPage() {
  const me = useMe();
  const canShifts = hasPermission(me.data, "shift.manage");
  const canPolicies = hasPermission(me.data, "attendance.policy.manage");
  const canAssign = hasPermission(me.data, "shift.assign.all");
  return (
    <div>
      <PageHeader
        title="Attendance set-up"
        description="Shifts say when work is expected; attendance policies say how a day is judged. Until both apply to someone, their days show times only."
      />
      <Tabs
        label="Attendance set-up"
        items={[
          { id: "check", label: "Set-up check", content: <SetupCheck /> },
          { id: "shifts", label: "Shifts", content: <ShiftsTab canManage={canShifts} /> },
          { id: "defaults", label: "Location defaults", content: <DefaultsTab canManage={canShifts} /> },
          { id: "policies", label: "Policies", content: <PoliciesTab canManage={canPolicies} /> },
          ...(canAssign ? [{ id: "assign", label: "Assign shifts", content: <AssignTab /> }] : []),
          { id: "locks", label: "Locks", content: <LocksTab canManage={canPolicies} /> },
          { id: "offices", label: "Office locations", content: <OfficeLocationsTab /> },
        ]}
      />
    </div>
  );
}

// --- set-up check ---------------------------------------------------------------------------------

function SetupCheck() {
  const setup = useQuery({
    queryKey: [...ATTENDANCE_KEY, "setup"],
    queryFn: () => unwrap(client.GET("/api/v1/attendance/setup")),
  });
  if (setup.isPending) return <Skeleton label="Checking attendance set-up" lines={4} />;
  if (setup.isError) {
    return (
      <ProblemMessage
        error={setup.error}
        fallback="We couldn't check the set-up."
        onRetry={() => void setup.refetch()}
      />
    );
  }
  const data = setup.data;
  const people = (list: typeof data.without_shift) =>
    list.length === 0 ? null : (
      <ul className="mt-2 flex flex-col gap-1 text-sm">
        {list.slice(0, 50).map((person) => (
          <li key={person.id}>
            <Link className="text-accent-700 underline" to={`/employees/${person.id}`}>
              {person.name}
            </Link>{" "}
            <span className="text-neutral-600">
              {[person.employee_code, person.location].filter(Boolean).join(" · ")}
            </span>
          </li>
        ))}
        {list.length > 50 ? <li className="text-neutral-600">and {list.length - 50} more</li> : null}
      </ul>
    );
  return (
    <div className="flex flex-col gap-4">
      <StatGrid label="Attendance set-up today">
        <Stat label="People in your scope" value={data.employees_in_scope} />
        <Stat label="Without a shift" value={data.without_shift.length} />
        <Stat label="Without a policy" value={data.without_policy.length} />
        <Stat label="Active shifts" value={data.active_shifts} />
      </StatGrid>
      {data.without_shift.length === 0 && data.without_policy.length === 0 ? (
        <Banner tone="success">Everyone in your scope has a shift and an attendance policy today.</Banner>
      ) : null}
      {data.without_shift.length > 0 ? (
        <Panel
          title="No shift today"
          description="Give them a shift, or give their location a default shift."
        >
          {people(data.without_shift)}
        </Panel>
      ) : null}
      {data.without_policy.length > 0 ? (
        <Panel
          title="No attendance policy today"
          description={
            data.organization_policy
              ? "Add a policy for their location or shift."
              : "Add an organization policy, or one for their location or shift."
          }
        >
          {people(data.without_policy)}
        </Panel>
      ) : null}
    </div>
  );
}

// --- shifts ---------------------------------------------------------------------------------------

function ShiftsTab({ canManage }: { canManage: boolean }) {
  const [showArchived, setShowArchived] = useState(false);
  const shifts = useShifts(showArchived);
  const [creating, setCreating] = useState(false);
  const [versioning, setVersioning] = useState<Shift | null>(null);
  const [editing, setEditing] = useState<Shift | null>(null);
  const items = shifts.data?.items ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Checkbox isSelected={showArchived} onChange={setShowArchived}>
          Show archived shifts
        </Checkbox>
        {canManage ? (
          <Button
            onPress={() => {
              setCreating(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add shift
          </Button>
        ) : null}
      </div>
      {shifts.isPending ? <Skeleton label="Loading shifts" lines={4} /> : null}
      {shifts.isError ? (
        <ProblemMessage
          error={shifts.error}
          fallback="We couldn't load the shifts."
          onRetry={() => void shifts.refetch()}
        />
      ) : null}
      {shifts.isSuccess && items.length === 0 ? (
        <EmptyState icon={CalendarClock} title="No shifts yet">
          Add each shift Sigvitas works, with its start and end times and weekly offs.
        </EmptyState>
      ) : null}
      {items.map((shift) => (
        <Panel
          key={shift.id}
          title={shift.name}
          description={shift.description ?? undefined}
          action={
            <div className="flex flex-wrap gap-2">
              {shift.status === "archived" ? <Badge tone="neutral">Archived</Badge> : null}
              {canManage && shift.status === "active" ? (
                <Button
                  size="sm"
                  variant="secondary"
                  onPress={() => {
                    setVersioning(shift);
                  }}
                >
                  New version
                </Button>
              ) : null}
              {canManage ? (
                <Button
                  size="sm"
                  variant="quiet"
                  onPress={() => {
                    setEditing(shift);
                  }}
                >
                  Edit<span className="sr-only"> {shift.name}</span>
                </Button>
              ) : null}
            </div>
          }
        >
          <p className="mb-3 font-mono text-xs text-neutral-700">{shift.code}</p>
          <DataTable
            label={`Versions of ${shift.name}`}
            rows={shift.versions}
            rowKey={(row) => row.id}
            columns={[
              {
                key: "dates",
                header: "In effect",
                primary: true,
                cell: (row) => inEffect(row.effective_from, row.effective_to),
              },
              {
                key: "times",
                header: "Times",
                cell: (row) =>
                  `${row.start_time.slice(0, 5)} to ${row.end_time.slice(0, 5)}${row.crosses_midnight ? " (next day)" : ""}`,
              },
              { key: "offs", header: "Weekly offs", cell: (row) => describeWeeklyOffs(row.weekly_offs) },
            ]}
          />
        </Panel>
      ))}
      {creating ? (
        <ShiftDialog
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
      {versioning ? (
        <ShiftDialog
          shift={versioning}
          onClose={() => {
            setVersioning(null);
          }}
        />
      ) : null}
      {editing ? (
        <EditShiftDialog
          shift={editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

/** Weekly offs: each weekday off every week, or only in some weeks of the month. */
function WeeklyOffFields({
  value,
  onChange,
}: {
  value: Record<string, { off: boolean; weeks: string }>;
  onChange: (next: Record<string, { off: boolean; weeks: string }>) => void;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-semibold text-neutral-800">Weekly offs</legend>
      <p className="text-sm text-neutral-600">
        Tick each day off. For a day off only in some weeks, list them (for example 2, 4 for the second and
        fourth).
      </p>
      {WEEKDAYS.map((day) => {
        const entry = value[day.id] ?? { off: false, weeks: "" };
        return (
          <div key={day.id} className="grid items-center gap-3 sm:grid-cols-[10rem_1fr]">
            <Checkbox
              isSelected={entry.off}
              onChange={(off) => {
                onChange({ ...value, [day.id]: { ...entry, off } });
              }}
            >
              {day.label}
            </Checkbox>
            {entry.off ? (
              <TextField
                label={`Weeks of the month for ${day.label} (optional)`}
                name={`weeks-${day.id}`}
                value={entry.weeks}
                onChange={(weeks) => {
                  onChange({ ...value, [day.id]: { ...entry, weeks } });
                }}
              />
            ) : null}
          </div>
        );
      })}
    </fieldset>
  );
}

function parseWeeklyOffs(value: Record<string, { off: boolean; weeks: string }>): WeeklyOff[] | string {
  const offs: WeeklyOff[] = [];
  for (const day of WEEKDAYS) {
    const entry = value[day.id];
    if (!entry?.off) continue;
    const weeks = entry.weeks
      .split(/[\s,]+/)
      .filter(Boolean)
      .map(Number);
    if (weeks.some((week) => !Number.isInteger(week) || week < 1 || week > 5)) {
      return `Weeks for ${day.label} must be numbers from 1 to 5.`;
    }
    offs.push({ weekday: Number(day.id), weeks: weeks.length > 0 ? [...new Set(weeks)] : null });
  }
  return offs;
}

function ShiftDialog({ shift, onClose }: { shift?: Shift; onClose: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [from, setFrom] = useState(localIsoDate());
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [offs, setOffs] = useState<Record<string, { off: boolean; weeks: string }>>({});
  const [local, setLocal] = useState<Record<string, string>>({});
  const save = useSave(
    () => {
      const weekly = parseWeeklyOffs(offs) as WeeklyOff[];
      const version = { effective_from: from, start_time: start, end_time: end, weekly_offs: weekly };
      return shift
        ? unwrap(
            client.POST("/api/v1/shifts/{shift_id}/versions", {
              params: { path: { shift_id: shift.id } },
              body: version,
            }),
          )
        : unwrap(
            client.POST("/api/v1/shifts", {
              body: {
                code,
                name,
                description: description.trim() === "" ? null : description.trim(),
                version,
              },
            }),
          );
    },
    shift ? "New shift version added" : "Shift added",
    onClose,
  );
  const errors = { ...fieldMessages(save.error), ...local };
  return (
    <Dialog
      title={shift ? `New version of ${shift.name}` : "Add shift"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          if (!shift && code.trim() === "") problems.code = "Enter a short code.";
          if (!shift && name.trim() === "") problems.name = "Enter a name.";
          if (!from) problems.effective_from = "Choose the date it takes effect.";
          if (!start) problems.start_time = "Enter the start time.";
          if (!end) problems.end_time = "Enter the end time.";
          const weekly = parseWeeklyOffs(offs);
          if (typeof weekly === "string") problems.weekly_offs = weekly;
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        {shift ? (
          <p className="text-sm text-neutral-700">
            The current version ends the day this one starts. Days before it keep the times they were judged
            with.
          </p>
        ) : null}
        <SaveError error={save.error} />
        {shift ? null : (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <TextField
                label="Code"
                name="code"
                value={code}
                onChange={setCode}
                maxLength={32}
                error={errors.code}
              />
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
            />
          </>
        )}
        <div className="grid gap-4 sm:grid-cols-3">
          <TextField
            label="In effect from"
            name="effective_from"
            type="date"
            value={from}
            onChange={setFrom}
            error={errors.effective_from ?? errors["version.effective_from"]}
          />
          <TextField
            label="Starts at"
            name="start_time"
            type="time"
            value={start}
            onChange={setStart}
            error={errors.start_time}
          />
          <TextField
            label="Ends at"
            name="end_time"
            type="time"
            value={end}
            onChange={setEnd}
            description="An end before the start means the next day."
            error={errors.end_time}
          />
        </div>
        <WeeklyOffFields value={offs} onChange={setOffs} />
        {errors.weekly_offs ? <p className="text-sm text-danger-700">{errors.weekly_offs}</p> : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            {shift ? "Add version" : "Add shift"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function EditShiftDialog({ shift, onClose }: { shift: Shift; onClose: () => void }) {
  const [name, setName] = useState(shift.name);
  const [description, setDescription] = useState(shift.description ?? "");
  const [status, setStatus] = useState<string>(shift.status);
  const archiving = shift.status === "active" && status === "archived";
  const save = useSave(
    () =>
      unwrap(
        client.PATCH("/api/v1/shifts/{shift_id}", {
          params: { path: { shift_id: shift.id }, header: { "If-Match": ifMatch(shift.version) } },
          body: {
            name,
            description: description.trim() === "" ? null : description.trim(),
            status: status as "active" | "archived",
          },
        }),
      ),
    "Shift updated",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={`Edit ${shift.name}`} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        <TextField
          label="Name"
          name="name"
          value={name}
          onChange={setName}
          maxLength={200}
          error={errors.name}
        />
        <TextArea
          label="Description (optional)"
          name="description"
          value={description}
          onChange={setDescription}
          maxLength={1000}
        />
        <Select
          label="Status"
          name="status"
          options={[
            { id: "active", label: "Active" },
            { id: "archived", label: "Archived (no new assignments)" },
          ]}
          value={status}
          onChange={setStatus}
          description="A shift still assigned to someone, now or later, cannot be archived."
        />
        {archiving ? (
          <Banner tone="warning">
            Archiving stops new assignments and location defaults with this shift. Its versions stay, so past
            days are still judged by them, and nothing is deleted. It is refused while anyone is assigned to
            it or a location uses it as its default, now or from a later date.
          </Banner>
        ) : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant={archiving ? "danger" : "primary"} isPending={save.isPending}>
            {archiving ? "Archive shift" : "Save changes"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

// --- location defaults ----------------------------------------------------------------------------

function DefaultsTab({ canManage }: { canManage: boolean }) {
  const defaults = useQuery({
    queryKey: [...ATTENDANCE_KEY, "defaults"],
    queryFn: () => unwrap(client.GET("/api/v1/location-default-shifts")),
  });
  const [adding, setAdding] = useState(false);
  const rows = defaults.data?.items ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-neutral-600">
          Everyone placed at a location works its default shift, unless they have a shift of their own.
        </p>
        {canManage ? (
          <Button
            onPress={() => {
              setAdding(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Set a default shift
          </Button>
        ) : null}
      </div>
      {defaults.isPending ? <Skeleton label="Loading default shifts" lines={3} /> : null}
      {defaults.isError ? (
        <ProblemMessage
          error={defaults.error}
          fallback="We couldn't load the default shifts."
          onRetry={() => void defaults.refetch()}
        />
      ) : null}
      {defaults.isSuccess && rows.length === 0 ? (
        <EmptyState icon={CalendarClock} title="No default shifts yet">
          Set a default shift for each location, so everyone placed there has one.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <DataTable
          label="Location default shifts"
          rows={rows}
          rowKey={(row) => row.id}
          columns={[
            { key: "location", header: "Location", primary: true, cell: (row) => row.location_name },
            { key: "shift", header: "Shift", cell: (row) => row.shift_name },
            {
              key: "dates",
              header: "In effect",
              cell: (row) => inEffect(row.effective_from, row.effective_to),
            },
          ]}
        />
      ) : null}
      {adding ? (
        <DefaultDialog
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
    </div>
  );
}

function DefaultDialog({ onClose }: { onClose: () => void }) {
  const locations = useLocations();
  const shifts = useShifts();
  const [location, setLocation] = useState<string | null>(null);
  const [shift, setShift] = useState<string | null>(null);
  const [from, setFrom] = useState(localIsoDate());
  const save = useSave(
    () =>
      unwrap(
        client.POST("/api/v1/location-default-shifts", {
          body: { location_id: location ?? "", shift_id: shift ?? "", effective_from: from },
        }),
      ),
    "Default shift set",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title="Set a location's default shift" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        <Select
          label="Location"
          name="location_id"
          placeholder="Choose"
          options={options(locations.data?.items)}
          value={location}
          onChange={setLocation}
          error={errors.location_id}
        />
        <Select
          label="Shift"
          name="shift_id"
          placeholder="Choose"
          options={options(shifts.data?.items)}
          value={shift}
          onChange={setShift}
          error={errors.shift_id}
        />
        <TextField
          label="From"
          name="effective_from"
          type="date"
          value={from}
          onChange={setFrom}
          error={errors.effective_from}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending} isDisabled={!location || !shift}>
            Set default shift
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

// --- policies -------------------------------------------------------------------------------------

function PoliciesTab({ canManage }: { canManage: boolean }) {
  const policies = useQuery({
    queryKey: [...ATTENDANCE_KEY, "policies"],
    queryFn: () => unwrap(client.GET("/api/v1/attendance-policies")),
  });
  const [adding, setAdding] = useState(false);
  const rows = policies.data?.items ?? [];
  const columns: Column<Policy>[] = [
    {
      key: "scope",
      header: "Applies to",
      primary: true,
      cell: (row) =>
        row.scope_type === "organization"
          ? "The organization"
          : `${row.scope_type === "shift" ? "Shift" : "Location"}: ${row.scope_name ?? ""}`,
    },
    { key: "dates", header: "In effect", cell: (row) => inEffect(row.effective_from, row.effective_to) },
    {
      key: "day",
      header: "Full and half day",
      cell: (row) =>
        `${row.full_day_min_minutes} / ${row.half_day_min_minutes} min (${row.duration_basis === "effective" ? "excluding breaks" : "including breaks"})`,
    },
    {
      key: "grace",
      header: "Grace",
      cell: (row) => `${row.grace_minutes} min late, ${row.early_departure_grace_minutes} min early`,
    },
    {
      key: "overtime",
      header: "Overtime",
      cell: (row) =>
        row.overtime_enabled
          ? `${row.overtime_basis === "beyond_full_day" ? "Beyond full day" : "Beyond shift end"}, from ${row.overtime_min_minutes ?? 0} min`
          : "Off",
    },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-neutral-600">
          The most specific policy applies: a shift's, else the location's, else the organization's. A new
          version ends the current one of the same scope on its start date.
        </p>
        {canManage ? (
          <Button
            onPress={() => {
              setAdding(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add policy version
          </Button>
        ) : null}
      </div>
      {policies.isPending ? <Skeleton label="Loading policies" lines={3} /> : null}
      {policies.isError ? (
        <ProblemMessage
          error={policies.error}
          fallback="We couldn't load the policies."
          onRetry={() => void policies.refetch()}
        />
      ) : null}
      {policies.isSuccess && rows.length === 0 ? (
        <EmptyState icon={CalendarClock} title="No attendance policies yet">
          Add the organization's policy, then any that differ for a location or shift.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <DataTable label="Attendance policies" columns={columns} rows={rows} rowKey={(row) => row.id} />
      ) : null}
      {adding ? (
        <PolicyDialog
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
    </div>
  );
}

const YES_NO = [
  { id: "yes", label: "Yes" },
  { id: "no", label: "No" },
];

function PolicyDialog({ onClose }: { onClose: () => void }) {
  const locations = useLocations();
  const shifts = useShifts();
  const [scope, setScope] = useState<string>("organization");
  const [scopeId, setScopeId] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({ effective_from: localIsoDate() });
  const [choices, setChoices] = useState<Record<string, string | null>>({
    duration_basis: null,
    breaks_count_as_work: null,
    break_excess_action: null,
    overtime_enabled: null,
    overtime_basis: null,
    overtime_requires_approval: null,
    allow_multiple_sessions: null,
  });
  const [local, setLocal] = useState<Record<string, string>>({});
  const number = (key: string) => Number(values[key]);
  const overtime = choices.overtime_enabled === "yes";
  const save = useSave(
    () =>
      unwrap(
        client.POST("/api/v1/attendance-policies", {
          body: {
            scope_type: scope as "organization",
            scope_id: scope === "organization" ? null : scopeId,
            effective_from: values.effective_from ?? "",
            grace_minutes: number("grace_minutes"),
            early_departure_grace_minutes: number("early_departure_grace_minutes"),
            duration_basis: choices.duration_basis as "effective",
            full_day_min_minutes: number("full_day_min_minutes"),
            half_day_min_minutes: number("half_day_min_minutes"),
            max_break_minutes: values.max_break_minutes ? number("max_break_minutes") : null,
            breaks_count_as_work: choices.breaks_count_as_work === "yes",
            break_excess_action: (choices.break_excess_action ?? "flag") as "flag",
            overtime_enabled: overtime,
            overtime_basis: overtime ? (choices.overtime_basis as "beyond_full_day") : null,
            overtime_min_minutes: overtime ? number("overtime_min_minutes") : null,
            overtime_requires_approval: overtime && choices.overtime_requires_approval === "yes",
            incomplete_after_minutes: number("incomplete_after_minutes"),
            allow_multiple_sessions: choices.allow_multiple_sessions === "yes",
          },
        }),
      ),
    "Policy version added",
    onClose,
  );
  const errors = { ...fieldMessages(save.error), ...local };
  const minutesField = (key: string, label: string, description?: string) => (
    <TextField
      label={label}
      name={key}
      value={values[key] ?? ""}
      onChange={(value) => {
        setValues((current) => ({ ...current, [key]: value }));
      }}
      inputMode="numeric"
      {...(description ? { description } : {})}
      error={errors[key]}
    />
  );
  const choice = (
    key: string,
    label: string,
    list: { id: string; label: string }[],
    description?: string,
  ) => (
    <Select
      label={label}
      name={key}
      placeholder="Choose"
      options={list}
      value={choices[key] ?? null}
      onChange={(value) => {
        setChoices((current) => ({ ...current, [key]: value }));
      }}
      {...(description ? { description } : {})}
      error={errors[key]}
    />
  );
  return (
    <Dialog title="Add an attendance policy version" isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          const required = [
            "grace_minutes",
            "early_departure_grace_minutes",
            "full_day_min_minutes",
            "half_day_min_minutes",
            "incomplete_after_minutes",
            ...(overtime ? ["overtime_min_minutes"] : []),
          ];
          for (const key of required) {
            const value = Number(values[key]);
            if (
              values[key] === undefined ||
              values[key] === "" ||
              !Number.isInteger(value) ||
              value < 0 ||
              value > 1440
            )
              problems[key] = "Enter whole minutes from 0 to 1440.";
          }
          if (values.max_break_minutes && !Number.isInteger(Number(values.max_break_minutes)))
            problems.max_break_minutes = "Enter whole minutes, or leave empty for no limit.";
          const needed = [
            "duration_basis",
            "breaks_count_as_work",
            "overtime_enabled",
            "allow_multiple_sessions",
            ...(overtime ? ["overtime_basis", "overtime_requires_approval"] : []),
            ...(values.max_break_minutes ? ["break_excess_action"] : []),
          ];
          for (const key of needed) if (!choices[key]) problems[key] = "Choose one.";
          if (scope !== "organization" && !scopeId) problems.scope_id = "Choose what this policy applies to.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        <p className="text-sm text-neutral-700">
          Every value is Sigvitas's to decide; nothing is filled in for you.
        </p>
        <SaveError error={save.error} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Select
            label="Applies to"
            name="scope_type"
            options={[
              { id: "organization", label: "The whole organization" },
              { id: "location", label: "One location" },
              { id: "shift", label: "One shift" },
            ]}
            value={scope}
            onChange={(value) => {
              setScope(value);
              setScopeId(null);
            }}
          />
          {scope === "organization" ? (
            <div />
          ) : (
            <Select
              label={scope === "location" ? "Location" : "Shift"}
              name="scope_id"
              placeholder="Choose"
              options={scope === "location" ? options(locations.data?.items) : options(shifts.data?.items)}
              value={scopeId}
              onChange={setScopeId}
              error={errors.scope_id}
            />
          )}
          <TextField
            label="In effect from"
            name="effective_from"
            type="date"
            value={values.effective_from ?? ""}
            onChange={(value) => {
              setValues((current) => ({ ...current, effective_from: value }));
            }}
            error={errors.effective_from}
          />
          <div />
          {minutesField(
            "grace_minutes",
            "Grace after shift start (minutes)",
            "Arriving later than this counts as late.",
          )}
          {minutesField("early_departure_grace_minutes", "Grace before shift end (minutes)")}
          {choice("duration_basis", "Judge the day on", [
            { id: "effective", label: "Time worked, excluding breaks" },
            { id: "gross", label: "Time clocked in, including breaks" },
          ])}
          {choice("breaks_count_as_work", "Breaks count as work", YES_NO)}
          {minutesField("full_day_min_minutes", "Full day from (minutes)")}
          {minutesField("half_day_min_minutes", "Half day from (minutes)")}
          {minutesField(
            "max_break_minutes",
            "Most break time (minutes, optional)",
            "Leave empty for no limit.",
          )}
          {values.max_break_minutes ? (
            choice("break_excess_action", "Breaks over the limit", [
              { id: "flag", label: "Flag the day only" },
              { id: "deduct", label: "Flag and deduct the excess" },
            ])
          ) : (
            <div />
          )}
          {minutesField(
            "incomplete_after_minutes",
            "No clock-out: mark incomplete after (minutes past shift end)",
          )}
          {choice("allow_multiple_sessions", "More than one session a day", YES_NO)}
          {choice("overtime_enabled", "Count overtime", YES_NO)}
          {overtime ? (
            choice("overtime_basis", "Overtime counts", [
              { id: "beyond_full_day", label: "Beyond the full-day minutes" },
              { id: "beyond_shift_end", label: "After the shift ends" },
            ])
          ) : (
            <div />
          )}
          {overtime ? minutesField("overtime_min_minutes", "Overtime counts from (minutes)") : null}
          {overtime ? choice("overtime_requires_approval", "Overtime needs approval", YES_NO) : null}
        </div>
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Add policy version
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

// --- assignments ----------------------------------------------------------------------------------

function AssignTab() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const search = useEmployeeSearch(null);
  const shifts = useShifts();
  const [chosen, setChosen] = useState<{ id: string; label: string }[]>([]);
  const [pick, setPick] = useState<string | null>(null);
  const [shift, setShift] = useState<string | null>(null);
  const [from, setFrom] = useState(localIsoDate());
  const assign = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/shift-assignments", {
          body: { employee_ids: chosen.map((item) => item.id), shift_id: shift ?? "", effective_from: from },
        }),
      ),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
      toast(
        result.skipped_not_permitted
          ? `${plural(result.assigned, "person", "people")} assigned; ${result.skipped_not_permitted} outside your scope left unchanged`
          : `${plural(result.assigned, "person", "people")} assigned`,
      );
      setChosen([]);
    },
  });
  const errors = fieldMessages(assign.error);
  return (
    <Form
      className="flex max-w-2xl flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!assign.isPending) assign.mutate();
      }}
    >
      <p className="text-sm text-neutral-600">
        A person's own shift replaces their location's default from the chosen date. Their current assignment
        ends that day.
      </p>
      {assign.isError && Object.keys(errors).length === 0 ? (
        <ProblemMessage error={assign.error} fallback="We couldn't assign the shift." />
      ) : null}
      <ComboBox
        label="Add a person"
        options={search.options}
        selectedKey={pick}
        onSelectionChange={(key) => {
          setPick(null);
          const option = search.options.find((item) => item.id === key);
          if (option && !chosen.some((item) => item.id === option.id)) {
            setChosen((current) => [...current, { id: option.id, label: option.label }]);
          }
          search.setText("");
        }}
        inputValue={search.text}
        onInputChange={search.setText}
        emptyText="No employee you can see matches."
      />
      {chosen.length > 0 ? (
        <ul aria-label="People to assign" className="flex flex-wrap gap-2">
          {chosen.map((item) => (
            <li key={item.id}>
              <Button
                size="sm"
                variant="secondary"
                aria-label={`Remove ${item.label}`}
                onPress={() => {
                  setChosen((current) => current.filter((row) => row.id !== item.id));
                }}
              >
                {item.label} ×
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-neutral-600">Nobody chosen yet.</p>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Select
          label="Shift"
          name="shift_id"
          placeholder="Choose"
          options={options(shifts.data?.items)}
          value={shift}
          onChange={setShift}
          error={errors.shift_id}
        />
        <TextField
          label="From"
          name="effective_from"
          type="date"
          value={from}
          onChange={setFrom}
          error={errors.effective_from}
        />
      </div>
      <div>
        <Button type="submit" isPending={assign.isPending} isDisabled={chosen.length === 0 || !shift}>
          Assign shift
        </Button>
      </div>
    </Form>
  );
}

// --- locks ----------------------------------------------------------------------------------------

function LocksTab({ canManage }: { canManage: boolean }) {
  const locks = useQuery({
    queryKey: [...ATTENDANCE_KEY, "locks"],
    queryFn: () => unwrap(client.GET("/api/v1/attendance/locks")),
  });
  const [adding, setAdding] = useState(false);
  const [releasing, setReleasing] = useState<Lock | null>(null);
  const rows = locks.data?.items ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-neutral-600">
          A locked period, such as a closed payroll month, can be corrected only by HR with access to
          everyone, and such corrections are flagged.
        </p>
        {canManage ? (
          <Button
            onPress={() => {
              setAdding(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Lock a period
          </Button>
        ) : null}
      </div>
      {locks.isPending ? <Skeleton label="Loading locks" lines={3} /> : null}
      {locks.isError ? (
        <ProblemMessage
          error={locks.error}
          fallback="We couldn't load the locks."
          onRetry={() => void locks.refetch()}
        />
      ) : null}
      {locks.isSuccess && rows.length === 0 ? (
        <EmptyState icon={CalendarClock} title="No locked periods">
          Lock a period once its attendance has been used, for example for payroll.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <DataTable
          label="Attendance locks"
          rows={rows}
          rowKey={(row) => row.id}
          columns={[
            {
              key: "period",
              header: "Period",
              primary: true,
              cell: (row) =>
                `${formatDate(row.period_start, { weekday: false })} to ${formatDate(row.period_end, { weekday: false })}`,
            },
            { key: "reason", header: "Reason", cell: (row) => row.reason },
            {
              key: "state",
              header: "State",
              cell: (row) =>
                row.released_at ? (
                  <Badge tone="neutral">Released: {row.release_reason}</Badge>
                ) : (
                  <Badge tone="warning">Locked</Badge>
                ),
            },
            ...(canManage
              ? [
                  {
                    key: "release",
                    header: "Action",
                    cell: (row: Lock) =>
                      row.released_at ? null : (
                        <Button
                          size="sm"
                          variant="quiet"
                          onPress={() => {
                            setReleasing(row);
                          }}
                        >
                          Release
                        </Button>
                      ),
                  },
                ]
              : []),
          ]}
        />
      ) : null}
      {adding ? (
        <LockDialog
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
      {releasing ? (
        <ReleaseDialog
          lock={releasing}
          onClose={() => {
            setReleasing(null);
          }}
        />
      ) : null}
    </div>
  );
}

function LockDialog({ onClose }: { onClose: () => void }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [reason, setReason] = useState("");
  const save = useSave(
    () =>
      unwrap(
        client.POST("/api/v1/attendance/locks", {
          body: { period_start: start, period_end: end, reason: reason.trim() },
        }),
      ),
    "Period locked",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title="Lock a period" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label="First day"
            name="period_start"
            type="date"
            value={start}
            onChange={setStart}
            error={errors.period_start}
          />
          <TextField
            label="Last day"
            name="period_end"
            type="date"
            value={end}
            onChange={setEnd}
            error={errors.period_end}
          />
        </div>
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={500}
          error={errors.reason}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            isPending={save.isPending}
            isDisabled={!start || !end || reason.trim() === ""}
          >
            Lock period
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function ReleaseDialog({ lock, onClose }: { lock: Lock; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const save = useSave(
    () =>
      unwrap(
        client.POST("/api/v1/attendance/locks/{lock_id}/release", {
          params: { path: { lock_id: lock.id } },
          body: { reason: reason.trim() },
        }),
      ),
    "Lock released",
    onClose,
  );
  return (
    <ConfirmDialog
      title="Release this lock?"
      isOpen
      onOpenChange={onClose}
      confirmLabel="Release lock"
      onConfirm={() => {
        if (reason.trim() !== "") save.mutate();
      }}
      isPending={save.isPending}
      tone="primary"
    >
      <p className="mb-3">
        {formatDate(lock.period_start)} to {formatDate(lock.period_end)} can then be corrected like any other
        dates.
      </p>
      <SaveError error={save.error} />
      <TextArea label="Reason" name="release_reason" value={reason} onChange={setReason} maxLength={500} />
    </ConfirmDialog>
  );
}
