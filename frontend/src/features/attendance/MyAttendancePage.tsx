/** The employee's own attendance: today's clock and the recorded days of a chosen period. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Banner } from "../../design-system/Banner";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Section } from "../../design-system/Section";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import { addDays, localIsoDate } from "../../lib/format";
import { ClockCard } from "./ClockCard";
import { DaysTable } from "./DaysTable";
import { ATTENDANCE_KEY } from "./queries";

function periods(today: string) {
  const [year, month] = today.split("-").map(Number);
  const firstOfMonth = `${today.slice(0, 7)}-01`;
  const previous = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 2, 1)).toISOString().slice(0, 10);
  return [
    { id: "7", label: "Last 7 days", from: addDays(today, -6), to: today },
    { id: "month", label: "This month", from: firstOfMonth, to: today },
    { id: "previous", label: "Last month", from: previous, to: addDays(firstOfMonth, -1) },
    { id: "31", label: "Last 31 days", from: addDays(today, -30), to: today },
  ];
}

export function MyAttendancePage() {
  const today = localIsoDate();
  const choices = periods(today);
  const [period, setPeriod] = useState("month");
  const range = choices.find((choice) => choice.id === period) ?? choices[1];
  const from = range?.from ?? today;
  const to = range?.to ?? today;
  const days = useQuery({
    queryKey: [...ATTENDANCE_KEY, "days", from, to],
    queryFn: () => unwrap(client.GET("/api/v1/me/attendance/days", { params: { query: { from, to } } })),
  });
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="My attendance"
        description="Clock in and out, take breaks, and see the days you have recorded."
      />
      <div className="max-w-2xl">
        <ClockCard />
      </div>
      <Section title="Recorded days">
        <div className="mb-4 max-w-xs">
          <Select label="Period" name="period" options={choices} value={period} onChange={setPeriod} />
        </div>
        <div className="mb-4">
          <Banner tone="info">
            Day status (present, late, half day) is shown once HR configures shifts and attendance policies.
            Until then you see the times recorded and the time between them.
          </Banner>
        </div>
        {days.isPending ? <Skeleton label="Loading your attendance" lines={5} /> : null}
        {days.isError ? (
          <ProblemMessage
            error={days.error}
            fallback="We couldn't load your attendance."
            onRetry={() => void days.refetch()}
          />
        ) : null}
        {days.isSuccess ? (
          <DaysTable days={days.data.items} timeZone={days.data.time_zone} label="Your attendance days" />
        ) : null}
      </Section>
    </div>
  );
}
