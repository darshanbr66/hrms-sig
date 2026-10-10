/** Holidays of the calendar the employee's location uses (product-requirements LV-7). */
import { useQuery } from "@tanstack/react-query";
import { CalendarDays } from "lucide-react";
import { useState } from "react";

import { useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { EmptyState } from "../../design-system/EmptyState";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import { formatDate, localIsoDate } from "../../lib/format";

export const HOLIDAYS_KEY = ["holidays"] as const;

export function MyHolidaysPage() {
  const me = useMe();
  const today = localIsoDate();
  const thisYear = Number(today.slice(0, 4));
  const [year, setYear] = useState(String(thisYear));
  const linked = Boolean(me.data?.employee_id);
  const holidays = useQuery({
    queryKey: [...HOLIDAYS_KEY, "me", year],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/me/holidays", {
          params: { query: { from: `${year}-01-01`, to: `${year}-12-31` } },
        }),
      ),
    enabled: linked,
  });
  const items = holidays.data?.items ?? [];
  const nextIndex = items.findIndex((item) => item.date >= today);
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader title="Holidays" description="The holiday calendar your work location uses." />
      {linked ? (
        <div className="max-w-xs">
          <Select
            label="Year"
            name="year"
            options={[thisYear, thisYear + 1].map((value) => ({ id: String(value), label: String(value) }))}
            value={year}
            onChange={setYear}
          />
        </div>
      ) : (
        <EmptyState icon={CalendarDays} title="No location to show holidays for">
          Your account is not linked to an employee record, so there is no work location and no holiday
          calendar.
        </EmptyState>
      )}
      {holidays.isPending && linked ? <Skeleton label="Loading holidays" lines={5} /> : null}
      {holidays.isError ? (
        <ProblemMessage
          error={holidays.error}
          fallback="We couldn't load the holidays."
          onRetry={() => void holidays.refetch()}
        />
      ) : null}
      {holidays.isSuccess && items.length === 0 ? (
        <EmptyState icon={CalendarDays} title={`No holidays published for ${year}`}>
          HR has not assigned a holiday calendar for your location this year, or it has no holidays yet.
        </EmptyState>
      ) : null}
      {items.length > 0 ? (
        <ul className="divide-y divide-neutral-200 rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0">
          {items.map((item, index) => {
            const past = item.date < today;
            return (
              <li
                key={item.date}
                className={`flex items-center justify-between gap-4 px-4 py-3 ${past ? "opacity-60" : ""}`}
              >
                <div className="min-w-0">
                  <p className="font-semibold text-neutral-900">{item.name}</p>
                  <p className="text-sm text-neutral-600">{formatDate(item.date)}</p>
                </div>
                <div className="flex flex-wrap justify-end gap-2">
                  {index === nextIndex ? <Badge tone="accent">Next</Badge> : null}
                  {item.is_optional ? <Badge tone="info">Optional</Badge> : null}
                  {past ? <Badge>Past</Badge> : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {items[0] ? <p className="text-xs text-neutral-600">Calendar: {items[0].calendar}</p> : null}
    </div>
  );
}
