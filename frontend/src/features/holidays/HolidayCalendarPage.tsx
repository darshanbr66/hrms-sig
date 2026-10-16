/**
 * One holiday calendar: its holidays, the working days declared on usual days off, and the
 * locations that use it. A declared working day (for example a Saturday management announces
 * as working) changes that one date for these locations; shifts and their weekly offs stay as
 * they are. Every change is audited.
 *
 * Record actions (docs/module-acceptance.md §9): an entry's name, and whether a holiday is
 * optional, are edited in place; its date and type are not, so a wrong one is removed and
 * added again. The calendar is renamed, or archived once no location uses it, and restored.
 * An archived calendar applies nowhere and is read-only until restored. The API enforces all
 * of this; hiding a button here is only a convenience.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, ArrowLeft, CalendarDays, MapPin, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useParams } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { ConfirmDialog, Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate } from "../../lib/format";
import { fieldMessages, ifMatch, problemText } from "../../lib/problem";
import { useLocations } from "../organization/queries";
import { HOLIDAYS_KEY } from "./MyHolidaysPage";

type Calendar = components["schemas"]["CalendarView"];
type Holiday = Calendar["holidays"][number];

export function HolidayCalendarPage() {
  const { calendarId = "" } = useParams();
  const key = [...HOLIDAYS_KEY, "calendar", calendarId];
  const calendar = useQuery({
    queryKey: key,
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/holiday-calendars/{calendar_id}", {
          params: { path: { calendar_id: calendarId } },
        }),
      ),
  });
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Holiday | null>(null);
  const [removing, setRemoving] = useState<Holiday | null>(null);
  const [calendarAction, setCalendarAction] = useState<"rename" | "archive" | "restore" | null>(null);

  if (calendar.isPending) return <Skeleton label="Loading the calendar" lines={6} />;
  if (calendar.isError) {
    return (
      <ProblemMessage
        error={calendar.error}
        fallback="We couldn't load this calendar."
        onRetry={() => void calendar.refetch()}
      />
    );
  }
  const data = calendar.data;
  const archived = data.status === "archived";
  const columns: Column<Holiday>[] = [
    { key: "date", header: "Date", primary: true, cell: (row) => formatDate(row.date) },
    { key: "name", header: "Name", cell: (row) => row.name },
    {
      key: "type",
      header: "Type",
      cell: (row) =>
        row.kind === "working_day" ? (
          <Badge tone="warning">Working day</Badge>
        ) : row.is_optional ? (
          <Badge tone="info">Optional</Badge>
        ) : (
          <Badge>Holiday</Badge>
        ),
    },
    ...(archived
      ? []
      : [
          {
            key: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Holiday) => (
              <div className="flex justify-end gap-1">
                <Button
                  variant="quiet"
                  size="sm"
                  onPress={() => {
                    setEditing(row);
                  }}
                >
                  <Pencil aria-hidden className="size-3.5" />
                  Edit<span className="sr-only"> {row.name}</span>
                </Button>
                <Button
                  variant="quiet"
                  size="sm"
                  onPress={() => {
                    setRemoving(row);
                  }}
                >
                  <Trash2 aria-hidden className="size-3.5" />
                  Remove<span className="sr-only"> {row.name}</span>
                </Button>
              </div>
            ),
          },
        ]),
  ];
  return (
    <div className="flex flex-col gap-6">
      <Link
        to="/setup/holiday-calendars"
        className="inline-flex items-center gap-1 text-sm text-accent-700 hover:underline"
      >
        <ArrowLeft aria-hidden className="size-4" />
        Holiday calendars
      </Link>
      <PageHeader
        title={`${data.name} (${data.year})`}
        description="Holidays, and working days announced on a usual day off. A working day changes only that date; shifts keep their weekly offs."
        action={
          <div className="flex flex-wrap gap-2">
            {archived ? (
              <Button
                variant="secondary"
                onPress={() => {
                  setCalendarAction("restore");
                }}
              >
                <ArchiveRestore aria-hidden className="size-4" />
                Restore calendar
              </Button>
            ) : (
              <>
                <Button
                  variant="secondary"
                  onPress={() => {
                    setCalendarAction("rename");
                  }}
                >
                  <Pencil aria-hidden className="size-4" />
                  Rename
                </Button>
                <Button
                  variant="secondary"
                  onPress={() => {
                    setCalendarAction("archive");
                  }}
                >
                  <Archive aria-hidden className="size-4" />
                  Archive
                </Button>
                <Button
                  onPress={() => {
                    setAdding(true);
                  }}
                >
                  <Plus aria-hidden className="size-4" />
                  Add a date
                </Button>
              </>
            )}
          </div>
        }
      />
      {archived ? (
        <Banner tone="warning">
          This calendar is archived. It applies to no location and cannot be changed. Restore it to change its
          dates or give it to a location.
        </Banner>
      ) : null}
      <Panel title="Holidays" padded={false}>
        <div className="p-4">
          {data.holidays.length === 0 ? (
            <EmptyState icon={CalendarDays} title="No dates in this calendar yet">
              Add each holiday Sigvitas observes in {data.year}, and any working day announced on a usual day
              off.
            </EmptyState>
          ) : (
            <DataTable label="Holidays" columns={columns} rows={data.holidays} rowKey={(row) => row.id} />
          )}
        </div>
      </Panel>
      <LocationsPanel calendar={data} queryKey={key} archived={archived} />
      {adding ? (
        <AddHolidayDialog
          calendar={data}
          queryKey={key}
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
      {editing ? (
        <EditHolidayDialog
          calendar={data}
          holiday={editing}
          queryKey={key}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
      {calendarAction === "rename" ? (
        <RenameCalendarDialog
          calendar={data}
          queryKey={key}
          onClose={() => {
            setCalendarAction(null);
          }}
        />
      ) : null}
      {calendarAction === "archive" || calendarAction === "restore" ? (
        <CalendarStatusDialog
          calendar={data}
          queryKey={key}
          onClose={() => {
            setCalendarAction(null);
          }}
        />
      ) : null}
      {removing ? (
        <RemoveHolidayDialog
          calendar={data}
          holiday={removing}
          queryKey={key}
          onClose={() => {
            setRemoving(null);
          }}
        />
      ) : null}
    </div>
  );
}

function useCalendarChange<T>(
  queryKey: readonly unknown[],
  change: () => Promise<T>,
  done: string,
  onDone?: () => void,
) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: change,
    onSuccess: async (result) => {
      queryClient.setQueryData(queryKey, result);
      await queryClient.invalidateQueries({ queryKey: HOLIDAYS_KEY });
      toast(done);
      onDone?.();
    },
  });
}

const KINDS = [
  { id: "holiday", label: "Holiday" },
  { id: "working_day", label: "Working day on a usual day off" },
];

function AddHolidayDialog({
  calendar,
  queryKey,
  onClose,
}: {
  calendar: Calendar;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const [date, setDate] = useState("");
  const [name, setName] = useState("");
  const [optional, setOptional] = useState(false);
  const [kind, setKind] = useState<string>("holiday");
  const working = kind === "working_day";
  const add = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.POST("/api/v1/holiday-calendars/{calendar_id}/holidays", {
          params: { path: { calendar_id: calendar.id }, header: { "If-Match": ifMatch(calendar.version) } },
          body: {
            date,
            name: name.trim(),
            is_optional: working ? false : optional,
            kind: working ? "working_day" : "holiday",
          },
        }),
      ),
    working ? "Working day added" : "Holiday added",
    onClose,
  );
  const errors = fieldMessages(add.error);
  return (
    <Dialog title="Add a date" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          add.mutate();
        }}
      >
        {add.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={add.error} fallback="We couldn't add the date. Try again." />
        ) : null}
        <Select
          label="Type"
          name="kind"
          options={KINDS}
          value={kind}
          onChange={setKind}
          {...(working
            ? {
                description:
                  "People at this calendar's locations work on this date even if their shift has it off. Use it for a Saturday announced as a working day.",
              }
            : {})}
        />
        <TextField
          label="Date"
          name="date"
          type="date"
          value={date}
          onChange={setDate}
          min={`${calendar.year}-01-01`}
          max={`${calendar.year}-12-31`}
          error={errors.date}
        />
        <TextField
          label="Name"
          name="name"
          value={name}
          onChange={setName}
          maxLength={200}
          error={errors.name}
        />
        {working ? null : (
          <Checkbox isSelected={optional} onChange={setOptional}>
            Optional holiday that employees may choose to take
          </Checkbox>
        )}
        {errors.is_optional ? <p className="text-sm text-danger-700">{errors.is_optional}</p> : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={add.isPending}>
            {working ? "Add working day" : "Add holiday"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function RemoveHolidayDialog({
  calendar,
  holiday,
  queryKey,
  onClose,
}: {
  calendar: Calendar;
  holiday: Holiday;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const working = holiday.kind === "working_day";
  const remove = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.DELETE("/api/v1/holiday-calendars/{calendar_id}/holidays/{holiday_id}", {
          params: {
            path: { calendar_id: calendar.id, holiday_id: holiday.id },
            header: { "If-Match": ifMatch(calendar.version) },
          },
        }),
      ),
    working ? "Working day removed" : "Holiday removed",
    onClose,
  );
  return (
    <ConfirmDialog
      title={`Remove ${holiday.name}?`}
      isOpen
      onOpenChange={onClose}
      confirmLabel={working ? "Remove working day" : "Remove holiday"}
      isPending={remove.isPending}
      onConfirm={() => {
        remove.mutate();
      }}
    >
      <p>
        {working
          ? `${formatDate(holiday.date)} goes back to what each shift says: a weekly off stays a day off for the locations using this calendar.`
          : `${formatDate(holiday.date)} will no longer be a holiday for the locations using this calendar.`}{" "}
        Attendance and leave day counts for that date change too, including past dates. The change is recorded
        in the audit log.
      </p>
      {remove.isError ? (
        <p className="mt-3 text-danger-700">{problemText(remove.error, "We couldn't remove it.")}</p>
      ) : null}
    </ConfirmDialog>
  );
}

function LocationsPanel({
  calendar,
  queryKey,
  archived,
}: {
  calendar: Calendar;
  queryKey: readonly unknown[];
  archived: boolean;
}) {
  const locations = useLocations();
  const [location, setLocation] = useState<string | null>(null);
  const assign = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.PUT("/api/v1/holiday-calendars/{calendar_id}/locations/{location_id}", {
          params: {
            path: { calendar_id: calendar.id, location_id: location ?? "" },
            header: { "If-Match": ifMatch(calendar.version) },
          },
        }),
      ),
    "Location now uses this calendar",
    () => {
      setLocation(null);
    },
  );
  const unassign = useMutation({
    mutationFn: (locationId: string) =>
      unwrap(
        client.DELETE("/api/v1/holiday-calendars/{calendar_id}/locations/{location_id}", {
          params: {
            path: { calendar_id: calendar.id, location_id: locationId },
            header: { "If-Match": ifMatch(calendar.version) },
          },
        }),
      ),
  });
  const queryClient = useQueryClient();
  const toast = useToast();
  const assigned = new Set(calendar.locations.map((item) => item.id));
  const choices = (locations.data?.items ?? [])
    .filter((item) => !assigned.has(item.id))
    .map((item) => ({ id: item.id, label: item.name }));
  return (
    <Panel
      title="Locations using this calendar"
      description={`Assigning a location replaces any other calendar it used for ${calendar.year}.`}
    >
      <div className="flex flex-col gap-4">
        {calendar.locations.length === 0 ? (
          <EmptyState icon={MapPin} title="No locations use this calendar">
            {archived
              ? "An archived calendar applies to no location."
              : "Employees see these holidays only once their location uses the calendar. Add a location below."}
          </EmptyState>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {calendar.locations.map((item) => (
              <li
                key={item.id}
                className="flex items-center gap-2 rounded-full border border-neutral-200 bg-neutral-50 py-1 pl-3 pr-1 text-sm"
              >
                {item.name}
                <Button
                  variant="quiet"
                  size="sm"
                  isPending={unassign.isPending && unassign.variables === item.id}
                  onPress={() => {
                    unassign.mutate(item.id, {
                      onSuccess: (result) => {
                        queryClient.setQueryData(queryKey, result);
                        void queryClient.invalidateQueries({ queryKey: HOLIDAYS_KEY });
                        toast(`${item.name} no longer uses this calendar`);
                      },
                    });
                  }}
                >
                  Remove<span className="sr-only"> {item.name}</span>
                </Button>
              </li>
            ))}
          </ul>
        )}
        {unassign.isError ? (
          <ProblemMessage error={unassign.error} fallback="We couldn't remove the location." />
        ) : null}
        {!archived && choices.length > 0 ? (
          <Form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (location) assign.mutate();
            }}
          >
            <div className="min-w-56 flex-1">
              <Select
                label="Add a location"
                name="location"
                options={choices}
                value={location}
                onChange={setLocation}
                placeholder="Choose a location"
              />
            </div>
            <Button type="submit" variant="secondary" isPending={assign.isPending} isDisabled={!location}>
              Use this calendar
            </Button>
          </Form>
        ) : null}
        {assign.isError ? (
          <ProblemMessage error={assign.error} fallback="We couldn't assign the location." />
        ) : null}
      </div>
    </Panel>
  );
}

function EditHolidayDialog({
  calendar,
  holiday,
  queryKey,
  onClose,
}: {
  calendar: Calendar;
  holiday: Holiday;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const working = holiday.kind === "working_day";
  const [name, setName] = useState(holiday.name);
  const [optional, setOptional] = useState(holiday.is_optional);
  const save = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.PATCH("/api/v1/holiday-calendars/{calendar_id}/holidays/{holiday_id}", {
          params: {
            path: { calendar_id: calendar.id, holiday_id: holiday.id },
            header: { "If-Match": ifMatch(calendar.version) },
          },
          body: working ? { name: name.trim() } : { name: name.trim(), is_optional: optional },
        }),
      ),
    working ? "Working day updated" : "Holiday updated",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title={working ? "Edit working day" : "Edit holiday"} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't save the change. Try again." />
        ) : null}
        <p className="text-sm text-neutral-700">
          {formatDate(holiday.date)}, {working ? "working day on a usual day off" : "holiday"}. To change the
          date or the type, remove this entry and add the right one.
        </p>
        <TextField
          label="Name"
          name="name"
          value={name}
          onChange={setName}
          maxLength={200}
          error={errors.name}
        />
        {working ? null : (
          <Checkbox isSelected={optional} onChange={setOptional}>
            Optional holiday that employees may choose to take
          </Checkbox>
        )}
        {errors.is_optional ? <p className="text-sm text-danger-700">{errors.is_optional}</p> : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Save changes
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function RenameCalendarDialog({
  calendar,
  queryKey,
  onClose,
}: {
  calendar: Calendar;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const [name, setName] = useState(calendar.name);
  const save = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.PATCH("/api/v1/holiday-calendars/{calendar_id}", {
          params: { path: { calendar_id: calendar.id }, header: { "If-Match": ifMatch(calendar.version) } },
          body: { name: name.trim() },
        }),
      ),
    "Calendar renamed",
    onClose,
  );
  const errors = fieldMessages(save.error);
  return (
    <Dialog title="Rename calendar" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't rename the calendar. Try again." />
        ) : null}
        <TextField
          label="Name"
          name="name"
          value={name}
          onChange={setName}
          maxLength={200}
          error={errors.name}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Save name
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function CalendarStatusDialog({
  calendar,
  queryKey,
  onClose,
}: {
  calendar: Calendar;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const archiving = calendar.status === "active";
  const change = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.PATCH("/api/v1/holiday-calendars/{calendar_id}", {
          params: { path: { calendar_id: calendar.id }, header: { "If-Match": ifMatch(calendar.version) } },
          body: { status: archiving ? "archived" : "active" },
        }),
      ),
    archiving ? "Calendar archived" : "Calendar restored",
    onClose,
  );
  const inUse = calendar.locations.length;
  return (
    <ConfirmDialog
      title={archiving ? `Archive ${calendar.name}?` : `Restore ${calendar.name}?`}
      isOpen
      onOpenChange={onClose}
      tone={archiving ? "danger" : "primary"}
      confirmLabel={archiving ? "Archive calendar" : "Restore calendar"}
      isPending={change.isPending}
      onConfirm={() => {
        change.mutate();
      }}
    >
      {archiving ? (
        <p>
          The calendar and its dates are kept, but it can no longer be changed or given to a location until it
          is restored. Nothing is deleted.
          {inUse > 0
            ? ` ${String(inUse)} ${inUse === 1 ? "location still uses" : "locations still use"} it, so archiving will be refused: remove them first or give them another calendar.`
            : ""}
        </p>
      ) : (
        <p>
          The calendar can be changed and given to locations again. Restoring it does not assign it anywhere.
        </p>
      )}
      {change.isError ? (
        <p className="mt-3 text-danger-700">
          {problemText(change.error, archiving ? "We couldn't archive it." : "We couldn't restore it.")}
        </p>
      ) : null}
    </ConfirmDialog>
  );
}
