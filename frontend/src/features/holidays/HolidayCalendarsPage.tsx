/**
 * Holiday calendars for HR admins (`holiday.manage`): one calendar per year, used by the
 * locations assigned to it. Which days are holidays is Sigvitas's decision; nothing is
 * pre-filled.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRange, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useNavigate } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { localIsoDate, plural } from "../../lib/format";
import { fieldMessages } from "../../lib/problem";
import { HOLIDAYS_KEY } from "./MyHolidaysPage";

type CalendarSummary = components["schemas"]["CalendarSummaryView"];

export function HolidayCalendarsPage() {
  const [creating, setCreating] = useState(false);
  const calendars = useQuery({
    queryKey: [...HOLIDAYS_KEY, "calendars"],
    queryFn: () => unwrap(client.GET("/api/v1/holiday-calendars")),
  });
  const items = calendars.data?.items ?? [];
  const columns: Column<CalendarSummary>[] = [
    {
      key: "name",
      header: "Calendar",
      primary: true,
      cell: (row) => (
        <Link className="hover:underline" to={`/setup/holiday-calendars/${row.id}`}>
          {row.name}
        </Link>
      ),
    },
    { key: "year", header: "Year", align: "end", cell: (row) => row.year },
    { key: "holidays", header: "Holidays", align: "end", cell: (row) => row.holiday_count },
    {
      key: "locations",
      header: "Locations",
      cell: (row) =>
        row.location_count === 0 ? (
          <Badge tone="warning">Not used by any location</Badge>
        ) : (
          plural(row.location_count, "location", "locations")
        ),
    },
    {
      key: "status",
      header: "Status",
      cell: (row) => (
        <Badge tone={row.status === "active" ? "success" : "neutral"}>
          {row.status === "active" ? "Active" : "Archived"}
        </Badge>
      ),
    },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Holiday calendars"
        description="Each location uses one calendar per year. Employees see the holidays of their location's calendar."
        action={
          <Button
            onPress={() => {
              setCreating(true);
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add calendar
          </Button>
        }
      />
      {calendars.isPending ? <Skeleton label="Loading holiday calendars" lines={4} /> : null}
      {calendars.isError ? (
        <ProblemMessage
          error={calendars.error}
          fallback="We couldn't load the holiday calendars."
          onRetry={() => void calendars.refetch()}
        />
      ) : null}
      {calendars.isSuccess && items.length === 0 ? (
        <EmptyState icon={CalendarRange} title="No holiday calendars yet">
          Add a calendar for the year, add its holidays, then choose the locations that use it.
        </EmptyState>
      ) : null}
      {items.length > 0 ? (
        <DataTable label="Holiday calendars" columns={columns} rows={items} rowKey={(row) => row.id} />
      ) : null}
      {creating ? (
        <CreateCalendarDialog
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
    </div>
  );
}

function CreateCalendarDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [name, setName] = useState("");
  const [year, setYear] = useState(localIsoDate().slice(0, 4));
  const create = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/holiday-calendars", { body: { name: name.trim(), year: Number(year) } })),
    onSuccess: async (calendar) => {
      await queryClient.invalidateQueries({ queryKey: HOLIDAYS_KEY });
      toast("Calendar added");
      void navigate(`/setup/holiday-calendars/${calendar.id}`);
    },
  });
  const errors = fieldMessages(create.error);
  return (
    <Dialog title="Add holiday calendar" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        {create.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={create.error} fallback="We couldn't add the calendar. Try again." />
        ) : null}
        <TextField
          label="Name"
          name="name"
          value={name}
          onChange={setName}
          maxLength={200}
          description="For example, the office or region it applies to."
          error={errors.name}
        />
        <TextField
          label="Year"
          name="year"
          value={year}
          onChange={setYear}
          inputMode="numeric"
          maxLength={4}
          error={errors.year}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={create.isPending}>
            Add calendar
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
