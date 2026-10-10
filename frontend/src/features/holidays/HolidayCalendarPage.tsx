/** One holiday calendar: its holidays and the locations that use it. Every change is audited. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CalendarDays, MapPin, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useParams } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { ConfirmDialog, Dialog } from "../../design-system/Dialog";
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
  const [removing, setRemoving] = useState<Holiday | null>(null);

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
  const columns: Column<Holiday>[] = [
    { key: "date", header: "Date", primary: true, cell: (row) => formatDate(row.date) },
    { key: "name", header: "Holiday", cell: (row) => row.name },
    {
      key: "type",
      header: "Type",
      cell: (row) => (row.is_optional ? <Badge tone="info">Optional</Badge> : <Badge>Holiday</Badge>),
    },
    {
      key: "actions",
      header: "Actions",
      align: "end",
      cell: (row) => (
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
      ),
    },
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
        description={
          data.status === "archived" ? "Archived: employees no longer see these holidays." : undefined
        }
        action={
          <Button
            onPress={() => {
              setAdding(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add holiday
          </Button>
        }
      />
      <Panel title="Holidays" padded={false}>
        <div className="p-4">
          {data.holidays.length === 0 ? (
            <EmptyState icon={CalendarDays} title="No holidays in this calendar yet">
              Add each holiday Sigvitas observes in {data.year}.
            </EmptyState>
          ) : (
            <DataTable label="Holidays" columns={columns} rows={data.holidays} rowKey={(row) => row.id} />
          )}
        </div>
      </Panel>
      <LocationsPanel calendar={data} queryKey={key} />
      {adding ? (
        <AddHolidayDialog
          calendar={data}
          queryKey={key}
          onClose={() => {
            setAdding(false);
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
  const add = useCalendarChange(
    queryKey,
    () =>
      unwrap(
        client.POST("/api/v1/holiday-calendars/{calendar_id}/holidays", {
          params: { path: { calendar_id: calendar.id }, header: { "If-Match": ifMatch(calendar.version) } },
          body: { date, name: name.trim(), is_optional: optional },
        }),
      ),
    "Holiday added",
    onClose,
  );
  const errors = fieldMessages(add.error);
  return (
    <Dialog title="Add holiday" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          add.mutate();
        }}
      >
        {add.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={add.error} fallback="We couldn't add the holiday. Try again." />
        ) : null}
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
        <Checkbox isSelected={optional} onChange={setOptional}>
          Optional holiday that employees may choose to take
        </Checkbox>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={add.isPending}>
            Add holiday
          </Button>
        </div>
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
    "Holiday removed",
    onClose,
  );
  return (
    <ConfirmDialog
      title={`Remove ${holiday.name}?`}
      isOpen
      onOpenChange={onClose}
      confirmLabel="Remove holiday"
      isPending={remove.isPending}
      onConfirm={() => {
        remove.mutate();
      }}
    >
      <p>
        {formatDate(holiday.date)} will no longer be a holiday for the locations using this calendar. The
        change is recorded in the audit log.
      </p>
      {remove.isError ? (
        <p className="mt-3 text-danger-700">{problemText(remove.error, "We couldn't remove it.")}</p>
      ) : null}
    </ConfirmDialog>
  );
}

function LocationsPanel({ calendar, queryKey }: { calendar: Calendar; queryKey: readonly unknown[] }) {
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
            Employees see these holidays only once their location uses the calendar.
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
        {choices.length > 0 ? (
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
