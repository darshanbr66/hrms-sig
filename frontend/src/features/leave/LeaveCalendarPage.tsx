/**
 * Who is away (product-requirements LV-8): leave of the people in the viewer's scope over a
 * chosen month, with the leave type and dates only, never the reason. Pending requests are
 * marked, so a manager sees what may still change.
 */
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { useSearchParams } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { EmptyState } from "../../design-system/EmptyState";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { localIsoDate } from "../../lib/format";
import { options, useDepartments, useLocations } from "../organization/queries";
import { LEAVE_KEY, STATUS_LABELS, STATUS_TONES, formatSpan, useLeaveTypes } from "./queries";

type Entry = components["schemas"]["LeaveCalendarEntry"];
const ANY = "any";

function monthRange(month: string): { from: string; to: string; label: string } {
  const [year = 2026, index = 1] = month.split("-").map(Number);
  const first = new Date(year, index - 1, 1);
  const last = new Date(year, index, 0);
  return {
    from: localIsoDate(first),
    to: localIsoDate(last),
    label: first.toLocaleDateString(undefined, { month: "long", year: "numeric" }),
  };
}

function shift(month: string, by: number): string {
  const [year = 2026, index = 1] = month.split("-").map(Number);
  const date = new Date(year, index - 1 + by, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

export function LeaveCalendarPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const month = params.get("month") ?? localIsoDate().slice(0, 7);
  const range = monthRange(month);
  const canReadOrg = hasPermission(me.data, "org.read");
  const types = useLeaveTypes(true);
  const departments = useDepartments(false, canReadOrg);
  const locations = useLocations(false, canReadOrg);
  const value = (key: string) => params.get(key) ?? ANY;
  const update = (key: string, next: string) => {
    const copy = new URLSearchParams(params);
    if (next === ANY) copy.delete(key);
    else copy.set(key, next);
    setParams(copy, { replace: true });
  };
  const calendar = useQuery({
    queryKey: [...LEAVE_KEY, "calendar", range.from, value("type"), value("department"), value("location")],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/leave/calendar", {
          params: {
            query: {
              from: range.from,
              to: range.to,
              ...(value("type") !== ANY ? { leave_type_id: value("type") } : {}),
              ...(value("department") !== ANY ? { department_id: value("department") } : {}),
              ...(value("location") !== ANY ? { location_id: value("location") } : {}),
            },
          },
        }),
      ),
  });
  const today = localIsoDate();
  const items = calendar.data?.items ?? [];
  const awayToday = items.filter(
    (item) => item.start_date <= today && item.end_date >= today && item.status !== "pending",
  );
  const columns: Column<Entry>[] = [
    { key: "employee", header: "Employee", primary: true, cell: (row) => row.employee.name },
    { key: "type", header: "Leave type", cell: (row) => row.leave_type.name },
    { key: "dates", header: "Dates", cell: (row) => formatSpan(row) },
    {
      key: "status",
      header: "Status",
      cell: (row) => <Badge tone={STATUS_TONES[row.status]}>{STATUS_LABELS[row.status]}</Badge>,
    },
  ];
  return (
    <div>
      <PageHeader
        title="Leave calendar"
        description="Who is away, with the leave type and dates. Reasons are never shown here."
      />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            aria-label="Previous month"
            onPress={() => {
              update("month", shift(month, -1));
            }}
          >
            <ChevronLeft aria-hidden className="size-4" />
          </Button>
          <h2 className="min-w-40 text-center text-base font-semibold text-neutral-900" aria-live="polite">
            {range.label}
          </h2>
          <Button
            variant="secondary"
            size="sm"
            aria-label="Next month"
            onPress={() => {
              update("month", shift(month, 1));
            }}
          >
            <ChevronRight aria-hidden className="size-4" />
          </Button>
        </div>
        <div className="grid flex-1 gap-3 sm:grid-cols-3">
          <Select
            label="Leave type"
            name="type"
            options={[
              { id: ANY, label: "All leave types" },
              ...(types.data?.items ?? []).map((t) => ({ id: t.id, label: t.name })),
            ]}
            value={value("type")}
            onChange={(next) => {
              update("type", next);
            }}
          />
          {canReadOrg ? (
            <>
              <Select
                label="Department"
                name="department"
                options={[{ id: ANY, label: "All departments" }, ...options(departments.data?.items)]}
                value={value("department")}
                onChange={(next) => {
                  update("department", next);
                }}
              />
              <Select
                label="Location"
                name="location"
                options={[{ id: ANY, label: "All locations" }, ...options(locations.data?.items)]}
                value={value("location")}
                onChange={(next) => {
                  update("location", next);
                }}
              />
            </>
          ) : null}
        </div>
      </div>
      {calendar.isPending ? <Skeleton label="Loading the leave calendar" lines={5} /> : null}
      {calendar.isError ? (
        <ProblemMessage
          error={calendar.error}
          fallback="We couldn't load the leave calendar."
          onRetry={() => void calendar.refetch()}
        />
      ) : null}
      {calendar.isSuccess ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-neutral-700">
            {awayToday.length === 0
              ? "Nobody in this view is on approved leave today."
              : `${awayToday.length} ${awayToday.length === 1 ? "person is" : "people are"} on approved leave today.`}
          </p>
          {calendar.data.truncated ? (
            <Banner tone="warning">There is more leave than this page shows. Narrow the filters.</Banner>
          ) : null}
          {items.length === 0 ? (
            <EmptyState icon={CalendarDays} title="No leave this month">
              Leave that is requested or approved in this month appears here.
            </EmptyState>
          ) : (
            <DataTable
              label={`Leave in ${range.label}`}
              columns={columns}
              rows={items}
              rowKey={(row) => row.request_id}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}
