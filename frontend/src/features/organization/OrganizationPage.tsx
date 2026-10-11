/**
 * Departments, designations and locations (docs/product-requirements.md EMP-2, §4.3 of the
 * database design). Everyone with `org.read` sees them; holders of `org.manage` add, rename
 * and archive them. Records are never deleted: archiving keeps them on job history and hides
 * them from new placements, and the API refuses it while anyone is still placed in the record.
 * Nothing is pre-filled: Sigvitas enters its own structure.
 *
 * The overview shows what is set up and, for people who read employee records, how many
 * employees each unit has today (counted by the API within the viewer's scope).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, CheckCircle2, Circle, MapPin, Plus, Shapes } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Form } from "react-aria-components";
import { Link, useSearchParams } from "react-router";

import { canSee } from "../../app/navigation";
import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button, ButtonLink } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { ComboBox } from "../../design-system/ComboBox";
import { Checkbox, SearchField, TextArea } from "../../design-system/Fields";
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
import { plural } from "../../lib/format";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { Headcounts } from "../employees/Headcounts";
import { PEOPLE_KEY, useEmployeeSearch, useStatistics } from "../employees/queries";
import {
  ORG_KEY,
  options,
  useDepartments,
  useDesignations,
  useLocations,
  type Department,
  type Designation,
  type Location,
} from "./queries";

type Statistics = components["schemas"]["EmployeeStatistics"];
type Kind = "departments" | "designations" | "locations";

const READS_PEOPLE = ["employee.profile.read.team", "employee.profile.read.all"];
const TABS = ["overview", "departments", "designations", "locations"] as const;
const STATUS_OPTIONS = [
  { id: "active", label: "Active" },
  { id: "archived", label: "Archived" },
];

function StatusBadge({ status }: { status: "active" | "archived" }) {
  return (
    <Badge tone={status === "active" ? "success" : "neutral"}>
      {status === "active" ? "Active" : "Archived"}
    </Badge>
  );
}

export function OrganizationPage() {
  const me = useMe();
  const canManage = hasPermission(me.data, "org.manage");
  const readsPeople = canSee(me.data, { anyOf: READS_PEOPLE });
  const stats = useStatistics(readsPeople);
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab = TABS.find((item) => item === requested) ?? "overview";
  const open = (key: string) => {
    const next = new URLSearchParams(params);
    if (key === "overview") next.delete("tab");
    else next.set("tab", key);
    setParams(next, { replace: true });
  };
  const headcounts = stats.data;
  return (
    <div>
      <PageHeader
        title="Organization"
        description="The departments, designations and locations employees are placed in. Archived records stay on job history but cannot be chosen for new placements."
      />
      <Tabs
        label="Organization records"
        selectedKey={tab}
        onSelectionChange={open}
        items={[
          {
            id: "overview",
            label: "Overview",
            content: <Overview canManage={canManage} readsPeople={readsPeople} onOpen={open} />,
          },
          {
            id: "departments",
            label: "Departments",
            content: <Departments canManage={canManage} headcounts={headcounts} />,
          },
          {
            id: "designations",
            label: "Designations",
            content: <Designations canManage={canManage} headcounts={headcounts} />,
          },
          {
            id: "locations",
            label: "Locations",
            content: <Locations canManage={canManage} headcounts={headcounts} />,
          },
        ]}
      />
    </div>
  );
}

// --- overview -----------------------------------------------------------------------------------

function Overview({
  canManage,
  readsPeople,
  onOpen,
}: {
  canManage: boolean;
  readsPeople: boolean;
  onOpen: (tab: Kind) => void;
}) {
  const me = useMe();
  const departments = useDepartments();
  const designations = useDesignations();
  const locations = useLocations();
  const stats = useStatistics(readsPeople);
  const lists = [locations, departments, designations];
  const failed = lists.find((query) => query.isError);
  if (failed) {
    return (
      <ProblemMessage
        error={failed.error}
        fallback="We couldn't load the organization."
        onRetry={() => {
          for (const query of lists) void query.refetch();
        }}
      />
    );
  }
  if (!locations.isSuccess || !departments.isSuccess || !designations.isSuccess) {
    return <Skeleton label="Loading the organization" lines={5} />;
  }
  const counts = {
    locations: locations.data.items.length,
    departments: departments.data.items.length,
    designations: designations.data.items.length,
  };
  const steps: { kind: Kind; title: string; detail: string; count: number }[] = [
    {
      kind: "locations",
      title: "Locations",
      detail: "Each office or site, with its time zone. Attendance work dates follow it.",
      count: counts.locations,
    },
    {
      kind: "departments",
      title: "Departments",
      detail: "The teams employees belong to. A department can sit inside another.",
      count: counts.departments,
    },
    {
      kind: "designations",
      title: "Designations",
      detail: "The job titles Sigvitas uses.",
      count: counts.designations,
    },
  ];
  const ready = steps.every((step) => step.count > 0);
  const canCreateEmployee = hasPermission(me.data, "employee.create");
  return (
    <div className="flex flex-col gap-6">
      <StatGrid label="Organization figures">
        <Stat label="Active locations" value={counts.locations} />
        <Stat label="Active departments" value={counts.departments} />
        <Stat label="Active designations" value={counts.designations} />
        {stats.isSuccess ? <Stat label="Employed today" value={stats.data.employed} to="/employees" /> : null}
      </StatGrid>

      <Panel
        title={ready ? "Organization set up" : "Set up your organization"}
        description={
          ready
            ? "Every employee record needs a location, department and designation. All three exist."
            : "Every employee record needs a location, department and designation, so add these first."
        }
        action={
          ready && canCreateEmployee ? (
            <ButtonLink to="/employees/new" size="sm">
              Add an employee
            </ButtonLink>
          ) : undefined
        }
      >
        <ol className="flex flex-col divide-y divide-neutral-200">
          {steps.map((step) => {
            const done = step.count > 0;
            return (
              <li key={step.kind} className="flex flex-wrap items-center gap-3 py-3 first:pt-0 last:pb-0">
                {done ? (
                  <CheckCircle2 aria-hidden className="size-5 shrink-0 text-success-700" />
                ) : (
                  <Circle aria-hidden className="size-5 shrink-0 text-neutral-400" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-neutral-900">
                    {step.title}
                    <span className="sr-only">{done ? " (done)" : " (to do)"}</span>
                  </p>
                  <p className="text-sm text-neutral-600">
                    {done ? `${plural(step.count, "active record", "active records")}. ` : "None yet. "}
                    {step.detail}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant={done || !canManage ? "secondary" : "primary"}
                  onPress={() => {
                    onOpen(step.kind);
                  }}
                >
                  {done || !canManage
                    ? `Open ${step.title.toLowerCase()}`
                    : `Add ${step.title.toLowerCase()}`}
                </Button>
              </li>
            );
          })}
        </ol>
      </Panel>

      {readsPeople ? <HeadcountPanels stats={stats} /> : null}
    </div>
  );
}

function HeadcountPanels({
  stats,
}: {
  stats: {
    data: Statistics | undefined;
    isPending: boolean;
    isError: boolean;
    error: unknown;
    refetch: () => unknown;
  };
}) {
  if (stats.isPending) return <Skeleton label="Loading employee counts" lines={4} />;
  if (stats.isError || !stats.data) {
    return (
      <ProblemMessage
        error={stats.error}
        fallback="We couldn't load the employee counts."
        onRetry={() => void stats.refetch()}
      />
    );
  }
  if (stats.data.employed === 0) {
    return (
      <EmptyState icon={Building2} title="No one employed yet">
        Counts per department, designation and location appear once employee records you can read exist.
      </EmptyState>
    );
  }
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Panel title="Employees by department">
        <Headcounts items={stats.data.by_department} filter="department" label="Employees by department" />
      </Panel>
      <Panel title="Employees by designation">
        <Headcounts items={stats.data.by_designation} filter="designation" label="Employees by designation" />
      </Panel>
      <Panel title="Employees by location">
        <Headcounts items={stats.data.by_location} filter="location" label="Employees by location" />
      </Panel>
    </div>
  );
}

// --- shared --------------------------------------------------------------------------------------

function matches(text: string, ...values: (string | null | undefined)[]): boolean {
  const needle = text.trim().toLowerCase();
  return needle === "" || values.some((value) => value?.toLowerCase().includes(needle));
}

/** The employees column: today's count within the viewer's scope, linking to the filtered list. */
function headcountColumn<T extends { id: string }>(
  headcounts: Statistics | undefined,
  items: keyof Pick<Statistics, "by_department" | "by_designation" | "by_location">,
  filter: "department" | "designation" | "location",
): Column<T>[] {
  if (!headcounts) return [];
  const byId = new Map(headcounts[items].map((item) => [item.id, item.employed]));
  return [
    {
      key: "employees",
      header: "Employees",
      align: "end",
      cell: (row) => {
        const count = byId.get(row.id) ?? 0;
        return count > 0 ? (
          <Link className="tabular text-accent-700 hover:underline" to={`/employees?${filter}=${row.id}`}>
            {count}
          </Link>
        ) : (
          <span className="tabular text-neutral-600">0</span>
        );
      },
    },
  ];
}

function editColumn<T extends { name: string }>(canManage: boolean, onEdit: (row: T) => void): Column<T>[] {
  if (!canManage) return [];
  return [
    {
      key: "actions",
      header: "Actions",
      align: "end",
      cell: (row) => (
        <Button
          variant="quiet"
          size="sm"
          onPress={() => {
            onEdit(row);
          }}
        >
          Edit<span className="sr-only"> {row.name}</span>
        </Button>
      ),
    },
  ];
}

function ListShell({
  title,
  action,
  query,
  text,
  onText,
  showArchived,
  onShowArchived,
  empty,
  children,
}: {
  title: string;
  action: ReactNode;
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  text: string;
  onText: (value: string) => void;
  showArchived: boolean;
  onShowArchived: (value: boolean) => void;
  empty: ReactNode | null;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-neutral-900">{title}</h2>
        {action}
      </div>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="sm:w-80">
          <SearchField
            label={`Search ${title.toLowerCase()}`}
            placeholder="Search by name or code"
            value={text}
            onChange={onText}
          />
        </div>
        <Checkbox isSelected={showArchived} onChange={onShowArchived}>
          Show archived
        </Checkbox>
      </div>
      {query.isPending ? <Skeleton label={`Loading ${title.toLowerCase()}`} lines={4} /> : null}
      {query.isError ? (
        <ProblemMessage
          error={query.error}
          fallback={`We couldn't load the ${title.toLowerCase()}.`}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {empty ?? children}
    </div>
  );
}

function NoMatches({ title }: { title: string }) {
  return (
    <EmptyState icon={Shapes} title={`No ${title} match this search`}>
      Change the search, or show archived records too.
    </EmptyState>
  );
}

function useSave<T>(save: () => Promise<T>, done: string, onDone: () => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: save,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ORG_KEY }),
        queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
      ]);
      toast(done);
      onDone();
    },
  });
}

function FormActions({
  pending,
  label,
  onCancel,
  danger = false,
}: {
  pending: boolean;
  label: string;
  onCancel: () => void;
  danger?: boolean;
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <Button variant="secondary" onPress={onCancel}>
        Cancel
      </Button>
      <Button type="submit" variant={danger ? "danger" : "primary"} isPending={pending}>
        {label}
      </Button>
    </div>
  );
}

function SaveError({ error }: { error: unknown }) {
  if (!error) return null;
  const fields = fieldMessages(error);
  return Object.keys(fields).length > 0 ? null : (
    <ProblemMessage error={error} fallback="We couldn't save the changes. Try again." />
  );
}

/** Shown in an edit dialog when the status is about to change to archived: what will happen,
 * before the person confirms with a button that says so. */
function ArchiveWarning({ kind, archiving }: { kind: string; archiving: boolean }) {
  if (!archiving) return null;
  return (
    <Banner tone="warning">
      Archiving hides this {kind} from new placements. Job history keeps it. It is refused while any employee
      who has not left is placed in it, now or from a future date.
    </Banner>
  );
}

// --- departments --------------------------------------------------------------------------------

function Departments({ canManage, headcounts }: { canManage: boolean; headcounts: Statistics | undefined }) {
  const [showArchived, setShowArchived] = useState(false);
  const [text, setText] = useState("");
  const departments = useDepartments(showArchived);
  const all = useDepartments(true);
  const [editing, setEditing] = useState<Department | "new" | null>(null);
  const items = (departments.data?.items ?? []).filter((row) =>
    matches(text, row.name, row.code, row.description),
  );
  const names = new Map((all.data?.items ?? []).map((item) => [item.id, item.name]));
  const columns: Column<Department>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (row) => (
        <span className="min-w-0">
          <span className="block">{row.name}</span>
          {row.description ? (
            <span className="block max-w-md truncate text-xs font-normal text-neutral-600">
              {row.description}
            </span>
          ) : null}
        </span>
      ),
    },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    {
      key: "parent",
      header: "Part of",
      cell: (row) => (row.parent_id ? (names.get(row.parent_id) ?? "Unknown") : "Top level"),
    },
    { key: "head", header: "Head", cell: (row) => row.head_name ?? "None" },
    ...headcountColumn<Department>(headcounts, "by_department", "department"),
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...editColumn<Department>(canManage, setEditing),
  ];
  const none = departments.isSuccess && departments.data.items.length === 0;
  return (
    <ListShell
      title="Departments"
      query={departments}
      text={text}
      onText={setText}
      showArchived={showArchived}
      onShowArchived={setShowArchived}
      action={
        canManage ? (
          <Button
            size="sm"
            onPress={() => {
              setEditing("new");
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add department
          </Button>
        ) : null
      }
      empty={
        none ? (
          <EmptyState icon={Building2} title="No departments yet">
            {canManage
              ? "Add the departments employees belong to. You can nest a department inside another."
              : "Departments appear here once HR adds them."}
          </EmptyState>
        ) : departments.isSuccess && items.length === 0 ? (
          <NoMatches title="departments" />
        ) : null
      }
    >
      {items.length > 0 ? (
        <DataTable label="Departments" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
      {editing ? (
        <DepartmentDialog
          department={editing === "new" ? null : editing}
          parents={(all.data?.items ?? []).filter(
            (item) => item.status === "active" && (editing === "new" || item.id !== editing.id),
          )}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
    </ListShell>
  );
}

function DepartmentDialog({
  department,
  parents,
  onClose,
}: {
  department: Department | null;
  parents: Department[];
  onClose: () => void;
}) {
  const [code, setCode] = useState(department?.code ?? "");
  const [name, setName] = useState(department?.name ?? "");
  const [description, setDescription] = useState(department?.description ?? "");
  const [parent, setParent] = useState<string>(department?.parent_id ?? "none");
  const [status, setStatus] = useState<string>(department?.status ?? "active");
  const me = useMe();
  // Choosing a head searches employee records, so it needs a permission to read them.
  const canPickHead = canSee(me.data, { anyOf: READS_PEOPLE });
  const initialHead =
    department?.head_employee_id && department.head_name
      ? { id: department.head_employee_id, name: department.head_name }
      : null;
  const [head, setHead] = useState<string | null>(initialHead?.id ?? null);
  const heads = useEmployeeSearch(initialHead);
  const headValue = canPickHead ? { head_employee_id: head } : {};
  const parentId = parent === "none" ? null : parent;
  const descriptionValue = description.trim() === "" ? null : description.trim();
  const archiving = department?.status === "active" && status === "archived";
  const save = useSave(
    () =>
      department
        ? unwrap(
            client.PATCH("/api/v1/departments/{department_id}", {
              params: {
                path: { department_id: department.id },
                header: { "If-Match": ifMatch(department.version) },
              },
              body: {
                name,
                description: descriptionValue,
                parent_id: parentId,
                ...headValue,
                status: status as "active" | "archived",
              },
            }),
          )
        : unwrap(
            client.POST("/api/v1/departments", {
              body: { code, name, description: descriptionValue, parent_id: parentId, ...headValue },
            }),
          ),
    archiving ? "Department archived" : department ? "Department updated" : "Department added",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={department ? `Edit ${department.name}` : "Add department"} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        {department ? null : (
          <TextField
            label="Code"
            name="code"
            value={code}
            onChange={setCode}
            maxLength={32}
            description="A short unique reference, such as ENG. It cannot change later."
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
        <TextArea
          label="Description (optional)"
          name="description"
          value={description}
          onChange={setDescription}
          maxLength={1000}
          description="What the department does, in a sentence or two."
          error={errors.description}
        />
        <Select
          label="Part of"
          name="parent"
          options={[{ id: "none", label: "Top level" }, ...options(parents)]}
          value={parent}
          onChange={setParent}
          error={errors.parent_id}
        />
        {canPickHead ? (
          <ComboBox
            label="Head (optional)"
            options={heads.options}
            selectedKey={head}
            onSelectionChange={setHead}
            inputValue={heads.text}
            onInputChange={(value) => {
              heads.setText(value);
              if (value === "") setHead(null);
            }}
            description="A current or future employee. Clear the box to have no head."
            emptyText="No current employee matches."
            error={errors.head_employee_id}
          />
        ) : null}
        {department ? (
          <Select label="Status" name="status" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
        ) : null}
        <ArchiveWarning kind="department" archiving={archiving} />
        <FormActions
          pending={save.isPending}
          label={archiving ? "Archive department" : department ? "Save changes" : "Add department"}
          danger={archiving}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}

// --- designations -------------------------------------------------------------------------------

function Designations({ canManage, headcounts }: { canManage: boolean; headcounts: Statistics | undefined }) {
  const [showArchived, setShowArchived] = useState(false);
  const [text, setText] = useState("");
  const designations = useDesignations(showArchived);
  const [editing, setEditing] = useState<Designation | "new" | null>(null);
  const items = (designations.data?.items ?? []).filter((row) => matches(text, row.name, row.code));
  const columns: Column<Designation>[] = [
    { key: "name", header: "Name", primary: true, cell: (row) => row.name },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    { key: "level", header: "Level", align: "end", cell: (row) => row.level ?? "None" },
    ...headcountColumn<Designation>(headcounts, "by_designation", "designation"),
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...editColumn<Designation>(canManage, setEditing),
  ];
  const none = designations.isSuccess && designations.data.items.length === 0;
  return (
    <ListShell
      title="Designations"
      query={designations}
      text={text}
      onText={setText}
      showArchived={showArchived}
      onShowArchived={setShowArchived}
      action={
        canManage ? (
          <Button
            size="sm"
            onPress={() => {
              setEditing("new");
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add designation
          </Button>
        ) : null
      }
      empty={
        none ? (
          <EmptyState icon={Shapes} title="No designations yet">
            {canManage
              ? "Add the job titles Sigvitas uses, such as the titles on offer letters."
              : "Designations appear here once HR adds them."}
          </EmptyState>
        ) : designations.isSuccess && items.length === 0 ? (
          <NoMatches title="designations" />
        ) : null
      }
    >
      {items.length > 0 ? (
        <DataTable label="Designations" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
      {editing ? (
        <DesignationDialog
          designation={editing === "new" ? null : editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
    </ListShell>
  );
}

function DesignationDialog({
  designation,
  onClose,
}: {
  designation: Designation | null;
  onClose: () => void;
}) {
  const [code, setCode] = useState(designation?.code ?? "");
  const [name, setName] = useState(designation?.name ?? "");
  const [level, setLevel] = useState(designation?.level?.toString() ?? "");
  const [status, setStatus] = useState<string>(designation?.status ?? "active");
  const [levelError, setLevelError] = useState<string | undefined>();
  const levelValue = level.trim() === "" ? null : Number(level);
  const archiving = designation?.status === "active" && status === "archived";
  const save = useSave(
    () =>
      designation
        ? unwrap(
            client.PATCH("/api/v1/designations/{designation_id}", {
              params: {
                path: { designation_id: designation.id },
                header: { "If-Match": ifMatch(designation.version) },
              },
              body: { name, level: levelValue, status: status as "active" | "archived" },
            }),
          )
        : unwrap(client.POST("/api/v1/designations", { body: { code, name, level: levelValue } })),
    archiving ? "Designation archived" : designation ? "Designation updated" : "Designation added",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog
      title={designation ? `Edit ${designation.name}` : "Add designation"}
      isOpen
      onOpenChange={onClose}
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (levelValue !== null && (!Number.isInteger(levelValue) || levelValue < 0 || levelValue > 100)) {
            setLevelError("Enter a whole number from 0 to 100, or leave it empty.");
            return;
          }
          setLevelError(undefined);
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        {designation ? null : (
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
        <TextField
          label="Level (optional)"
          name="level"
          value={level}
          onChange={setLevel}
          inputMode="numeric"
          description="A number for ordering designations by seniority, if Sigvitas uses one."
          error={levelError ?? errors.level}
        />
        {designation ? (
          <Select label="Status" name="status" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
        ) : null}
        <ArchiveWarning kind="designation" archiving={archiving} />
        <FormActions
          pending={save.isPending}
          label={archiving ? "Archive designation" : designation ? "Save changes" : "Add designation"}
          danger={archiving}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}

// --- locations ----------------------------------------------------------------------------------

function Locations({ canManage, headcounts }: { canManage: boolean; headcounts: Statistics | undefined }) {
  const [showArchived, setShowArchived] = useState(false);
  const [text, setText] = useState("");
  const locations = useLocations(showArchived);
  const [editing, setEditing] = useState<Location | "new" | null>(null);
  const items = (locations.data?.items ?? []).filter((row) =>
    matches(text, row.name, row.code, row.time_zone, row.country_code),
  );
  const columns: Column<Location>[] = [
    { key: "name", header: "Name", primary: true, cell: (row) => row.name },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    { key: "zone", header: "Time zone", cell: (row) => row.time_zone },
    { key: "country", header: "Country", cell: (row) => row.country_code },
    ...headcountColumn<Location>(headcounts, "by_location", "location"),
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...editColumn<Location>(canManage, setEditing),
  ];
  const none = locations.isSuccess && locations.data.items.length === 0;
  return (
    <ListShell
      title="Locations"
      query={locations}
      text={text}
      onText={setText}
      showArchived={showArchived}
      onShowArchived={setShowArchived}
      action={
        canManage ? (
          <Button
            size="sm"
            onPress={() => {
              setEditing("new");
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add location
          </Button>
        ) : null
      }
      empty={
        none ? (
          <EmptyState icon={MapPin} title="No locations yet">
            {canManage
              ? "Add each office or site with its time zone. Attendance work dates use the location's time zone."
              : "Locations appear here once HR adds them."}
          </EmptyState>
        ) : locations.isSuccess && items.length === 0 ? (
          <NoMatches title="locations" />
        ) : null
      }
    >
      {items.length > 0 ? (
        <DataTable label="Locations" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
      {editing ? (
        <LocationDialog
          location={editing === "new" ? null : editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
    </ListShell>
  );
}

function LocationDialog({ location, onClose }: { location: Location | null; onClose: () => void }) {
  const [code, setCode] = useState(location?.code ?? "");
  const [name, setName] = useState(location?.name ?? "");
  const [timeZone, setTimeZone] = useState(location?.time_zone ?? "");
  const [country, setCountry] = useState(location?.country_code ?? "");
  const [address, setAddress] = useState(location?.address ?? "");
  const [status, setStatus] = useState<string>(location?.status ?? "active");
  const archiving = location?.status === "active" && status === "archived";
  const body = {
    name,
    time_zone: timeZone.trim(),
    country_code: country.trim().toUpperCase(),
    address: address.trim() === "" ? null : address.trim(),
  };
  const save = useSave(
    () =>
      location
        ? unwrap(
            client.PATCH("/api/v1/locations/{location_id}", {
              params: {
                path: { location_id: location.id },
                header: { "If-Match": ifMatch(location.version) },
              },
              body: { ...body, status: status as "active" | "archived" },
            }),
          )
        : unwrap(client.POST("/api/v1/locations", { body: { ...body, code } })),
    archiving ? "Location archived" : location ? "Location updated" : "Location added",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={location ? `Edit ${location.name}` : "Add location"} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <SaveError error={save.error} />
        {location ? null : (
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
        <TextField
          label="Time zone"
          name="time_zone"
          value={timeZone}
          onChange={setTimeZone}
          maxLength={64}
          description="The IANA name, for example Asia/Kolkata."
          error={errors.time_zone}
        />
        <TextField
          label="Country code"
          name="country_code"
          value={country}
          onChange={setCountry}
          maxLength={2}
          description="Two letters, for example IN."
          error={errors.country_code}
        />
        <TextArea
          label="Address (optional)"
          name="address"
          value={address}
          onChange={setAddress}
          maxLength={1000}
          error={errors.address}
        />
        {location ? (
          <Select label="Status" name="status" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
        ) : null}
        <ArchiveWarning kind="location" archiving={archiving} />
        <FormActions
          pending={save.isPending}
          label={archiving ? "Archive location" : location ? "Save changes" : "Add location"}
          danger={archiving}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}
