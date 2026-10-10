/**
 * Departments, designations and locations (docs/product-requirements.md EMP-2, §4.3 of the
 * database design). Everyone with `org.read` sees them; holders of `org.manage` add, rename
 * and archive them. Records are never deleted: archiving keeps them on job history and hides
 * them from new placements. Nothing is pre-filled: Sigvitas enters its own structure.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, MapPin, Plus, Shapes } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Form } from "react-aria-components";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs } from "../../design-system/Tabs";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { fieldMessages, ifMatch } from "../../lib/problem";
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
  const [showArchived, setShowArchived] = useState(false);
  return (
    <div>
      <PageHeader
        title="Organization"
        description="The departments, designations and locations employees are placed in. Archived records stay on job history but cannot be chosen for new placements."
      />
      <div className="mb-4">
        <Checkbox isSelected={showArchived} onChange={setShowArchived}>
          Show archived records
        </Checkbox>
      </div>
      <Tabs
        label="Organization records"
        items={[
          {
            id: "departments",
            label: "Departments",
            content: <Departments canManage={canManage} showArchived={showArchived} />,
          },
          {
            id: "designations",
            label: "Designations",
            content: <Designations canManage={canManage} showArchived={showArchived} />,
          },
          {
            id: "locations",
            label: "Locations",
            content: <Locations canManage={canManage} showArchived={showArchived} />,
          },
        ]}
      />
    </div>
  );
}

// --- shared --------------------------------------------------------------------------------------

function ListShell({
  title,
  action,
  query,
  empty,
  children,
}: {
  title: string;
  action: ReactNode;
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  empty: ReactNode | null;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-neutral-900">{title}</h2>
        {action}
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

function useSave<T>(save: () => Promise<T>, done: string, onDone: () => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: save,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ORG_KEY });
      toast(done);
      onDone();
    },
  });
}

function FormActions({
  pending,
  label,
  onCancel,
}: {
  pending: boolean;
  label: string;
  onCancel: () => void;
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <Button variant="secondary" onPress={onCancel}>
        Cancel
      </Button>
      <Button type="submit" isPending={pending}>
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

// --- departments --------------------------------------------------------------------------------

function Departments({ canManage, showArchived }: { canManage: boolean; showArchived: boolean }) {
  const departments = useDepartments(showArchived);
  const all = useDepartments(true);
  const [editing, setEditing] = useState<Department | "new" | null>(null);
  const items = departments.data?.items ?? [];
  const names = new Map((all.data?.items ?? []).map((item) => [item.id, item.name]));
  const columns: Column<Department>[] = [
    { key: "name", header: "Name", primary: true, cell: (row) => row.name },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    {
      key: "parent",
      header: "Part of",
      cell: (row) => (row.parent_id ? (names.get(row.parent_id) ?? "Unknown") : "Top level"),
    },
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...(canManage
      ? [
          {
            key: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Department) => (
              <Button
                variant="quiet"
                size="sm"
                onPress={() => {
                  setEditing(row);
                }}
              >
                Edit<span className="sr-only"> {row.name}</span>
              </Button>
            ),
          },
        ]
      : []),
  ];
  return (
    <ListShell
      title="Departments"
      query={departments}
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
        departments.isSuccess && items.length === 0 ? (
          <EmptyState icon={Building2} title="No departments yet">
            {canManage
              ? "Add the departments employees belong to. You can nest a department inside another."
              : "Departments appear here once HR adds them."}
          </EmptyState>
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
  const [parent, setParent] = useState<string>(department?.parent_id ?? "none");
  const [status, setStatus] = useState<string>(department?.status ?? "active");
  const parentId = parent === "none" ? null : parent;
  const save = useSave(
    () =>
      department
        ? unwrap(
            client.PATCH("/api/v1/departments/{department_id}", {
              params: {
                path: { department_id: department.id },
                header: { "If-Match": ifMatch(department.version) },
              },
              body: { name, parent_id: parentId, status: status as "active" | "archived" },
            }),
          )
        : unwrap(client.POST("/api/v1/departments", { body: { code, name, parent_id: parentId } })),
    department ? "Department updated" : "Department added",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={department ? `Edit ${department.name}` : "Add department"} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
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
        <Select
          label="Part of"
          name="parent"
          options={[{ id: "none", label: "Top level" }, ...options(parents)]}
          value={parent}
          onChange={setParent}
          error={errors.parent_id}
        />
        {department ? (
          <Select label="Status" name="status" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
        ) : null}
        <FormActions
          pending={save.isPending}
          label={department ? "Save changes" : "Add department"}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}

// --- designations -------------------------------------------------------------------------------

function Designations({ canManage, showArchived }: { canManage: boolean; showArchived: boolean }) {
  const designations = useDesignations(showArchived);
  const [editing, setEditing] = useState<Designation | "new" | null>(null);
  const items = designations.data?.items ?? [];
  const columns: Column<Designation>[] = [
    { key: "name", header: "Name", primary: true, cell: (row) => row.name },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    { key: "level", header: "Level", align: "end", cell: (row) => row.level ?? "None" },
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...(canManage
      ? [
          {
            key: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Designation) => (
              <Button
                variant="quiet"
                size="sm"
                onPress={() => {
                  setEditing(row);
                }}
              >
                Edit<span className="sr-only"> {row.name}</span>
              </Button>
            ),
          },
        ]
      : []),
  ];
  return (
    <ListShell
      title="Designations"
      query={designations}
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
        designations.isSuccess && items.length === 0 ? (
          <EmptyState icon={Shapes} title="No designations yet">
            {canManage
              ? "Add the job titles Sigvitas uses, such as the titles on offer letters."
              : "Designations appear here once HR adds them."}
          </EmptyState>
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
    designation ? "Designation updated" : "Designation added",
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
          save.mutate();
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
        <FormActions
          pending={save.isPending}
          label={designation ? "Save changes" : "Add designation"}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}

// --- locations ----------------------------------------------------------------------------------

function Locations({ canManage, showArchived }: { canManage: boolean; showArchived: boolean }) {
  const locations = useLocations(showArchived);
  const [editing, setEditing] = useState<Location | "new" | null>(null);
  const items = locations.data?.items ?? [];
  const columns: Column<Location>[] = [
    { key: "name", header: "Name", primary: true, cell: (row) => row.name },
    { key: "code", header: "Code", cell: (row) => <code className="font-mono text-xs">{row.code}</code> },
    { key: "zone", header: "Time zone", cell: (row) => row.time_zone },
    { key: "country", header: "Country", cell: (row) => row.country_code },
    { key: "status", header: "Status", cell: (row) => <StatusBadge status={row.status} /> },
    ...(canManage
      ? [
          {
            key: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Location) => (
              <Button
                variant="quiet"
                size="sm"
                onPress={() => {
                  setEditing(row);
                }}
              >
                Edit<span className="sr-only"> {row.name}</span>
              </Button>
            ),
          },
        ]
      : []),
  ];
  return (
    <ListShell
      title="Locations"
      query={locations}
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
        locations.isSuccess && items.length === 0 ? (
          <EmptyState icon={MapPin} title="No locations yet">
            {canManage
              ? "Add each office or site with its time zone. Attendance work dates use the location's time zone."
              : "Locations appear here once HR adds them."}
          </EmptyState>
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
    location ? "Location updated" : "Location added",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={location ? `Edit ${location.name}` : "Add location"} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
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
        <TextField
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
        <FormActions
          pending={save.isPending}
          label={location ? "Save changes" : "Add location"}
          onCancel={onClose}
        />
      </Form>
    </Dialog>
  );
}
