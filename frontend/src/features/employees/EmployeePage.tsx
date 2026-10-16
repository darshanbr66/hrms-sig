/**
 * One employee's record (authorization-model.md §3.2). Each section appears only when the
 * viewer holds a permission for it, and the API decides every read and change: a manager sees
 * the team member's job details, HR also personal details (every read audited), job and status
 * changes, and the linked HRMS account.
 */
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { canSee } from "../../app/navigation";
import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs, type TabItem } from "../../design-system/Tabs";
import { client, unwrap } from "../../lib/api";
import { addDays, localIsoDate } from "../../lib/format";
import { ApiError } from "../../lib/problem";
import { CorrectionDialog } from "../attendance/CorrectionDialog";
import { DaysTable, type Day } from "../attendance/DaysTable";
import { DocumentList } from "../documents/DocumentList";
import { LeaveBalances } from "../leave/LeaveBalances";
import { AccountSection, JobHistorySection, OverviewSection, StatusSection } from "./EmployeeSections";
import { EmergencyContacts } from "./EmergencyContacts";
import { PersonalDetails } from "./PersonalDetails";
import { PEOPLE_KEY, STATUS_LABELS, STATUS_TONES, useEmployee } from "./queries";

export function EmployeePage() {
  const { employeeId = "" } = useParams();
  const me = useMe();
  const employee = useEmployee(employeeId);

  if (employee.isPending) return <Skeleton label="Loading the employee record" lines={6} />;
  if (employee.isError) {
    const missing = employee.error instanceof ApiError && employee.error.is("not-found");
    return (
      <div>
        <BackLink />
        <PageHeader title={missing ? "Employee not found" : "Employee"} />
        {missing ? (
          <p className="text-sm text-neutral-700">
            This record does not exist, or it is outside the people you can see.
          </p>
        ) : (
          <ProblemMessage
            error={employee.error}
            fallback="We couldn't load this employee."
            onRetry={() => void employee.refetch()}
          />
        )}
      </div>
    );
  }

  const record = employee.data;
  const permissions = me.data;
  const isSelf = permissions?.employee_id === record.id;
  const tabs: TabItem[] = [
    { id: "overview", label: "Overview", content: <OverviewSection employee={record} /> },
    { id: "jobs", label: "Job history", content: <JobHistorySection employee={record} /> },
    { id: "status", label: "Status", content: <StatusSection employee={record} /> },
  ];
  if (hasPermission(permissions, "employee.personal.read.all") && !isSelf) {
    tabs.push({
      id: "personal",
      label: "Personal",
      content: (
        <div className="flex flex-col gap-6">
          <PersonalDetails
            employeeId={record.id}
            canEdit={hasPermission(permissions, "employee.personal.update.all")}
          />
          <EmergencyContacts
            employeeId={record.id}
            canEdit={hasPermission(permissions, "employee.personal.update.all")}
          />
        </div>
      ),
    });
  }
  if (canSee(permissions, { anyOf: ["attendance.read.team", "attendance.read.all"] })) {
    tabs.push({
      id: "attendance",
      label: "Attendance",
      content: <EmployeeAttendance employeeId={record.id} />,
    });
  }
  if (canSee(permissions, { anyOf: ["leave.read.team", "leave.read.all"] })) {
    tabs.push({ id: "leave", label: "Leave", content: <LeaveBalances employeeId={record.id} /> });
  }
  if (
    canSee(permissions, { anyOf: ["document.read.team", "document.read.all", "document.manage.all"] }) &&
    !isSelf
  ) {
    tabs.push({
      id: "documents",
      label: "Documents",
      content: (
        <DocumentList
          owner={{ kind: "employee", id: record.id }}
          canUpload={hasPermission(permissions, "document.manage.all")}
          uploadableCategory={(category) => category.status === "active"}
          emptyText="Documents uploaded for this employee appear here, in the categories you may see."
        />
      ),
    });
  }
  if (hasPermission(permissions, "user.read.all")) {
    tabs.push({ id: "account", label: "HRMS account", content: <AccountSection employee={record} /> });
  }

  const job = record.job;
  return (
    <div>
      <BackLink />
      <header className="mb-6 flex flex-wrap items-start gap-4">
        <Avatar name={record.name} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-[1.375rem] font-semibold text-neutral-900">{record.name}</h1>
            <Badge tone={STATUS_TONES[record.status]}>{STATUS_LABELS[record.status]}</Badge>
          </div>
          <p className="mt-1 text-sm text-neutral-600">
            <span className="font-mono">{record.employee_code}</span>
            {job
              ? ` · ${job.designation.name} · ${job.department.name} · ${job.location.name}`
              : " · Not placed"}
          </p>
          {job?.manager ? (
            <p className="mt-1 text-sm text-neutral-600">
              Reports to{" "}
              <Link className="text-accent-700 underline" to={`/employees/${job.manager.id}`}>
                {job.manager.name}
              </Link>
            </p>
          ) : null}
        </div>
      </header>
      <Tabs label="Employee record" items={tabs} />
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/employees"
      className="mb-4 inline-flex items-center gap-1 text-sm text-accent-700 hover:underline"
    >
      <ArrowLeft aria-hidden className="size-4" />
      Employees
    </Link>
  );
}

function EmployeeAttendance({ employeeId }: { employeeId: string }) {
  const me = useMe();
  const canRecord =
    hasPermission(me.data, "attendance.correction.create.all") && me.data?.employee_id !== employeeId;
  const [correcting, setCorrecting] = useState<Day | null>(null);
  const to = localIsoDate();
  const from = addDays(to, -30);
  const days = useQuery({
    queryKey: [...PEOPLE_KEY, "attendance", employeeId, from, to],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees/{employee_id}/attendance/days", {
          params: { path: { employee_id: employeeId }, query: { from, to } },
        }),
      ),
  });
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-neutral-600">The last 31 days of recorded attendance.</p>
      {days.isPending ? <Skeleton label="Loading attendance" lines={4} /> : null}
      {days.isError ? (
        <ProblemMessage
          error={days.error}
          fallback="We couldn't load the attendance."
          onRetry={() => void days.refetch()}
        />
      ) : null}
      {days.isSuccess ? (
        <DaysTable
          days={days.data.items}
          timeZone={days.data.time_zone}
          label="Attendance days"
          {...(canRecord
            ? {
                onCorrect: (day: Day) => {
                  setCorrecting(day);
                },
              }
            : {})}
        />
      ) : null}
      {correcting && days.isSuccess ? (
        <CorrectionDialog
          day={correcting}
          workDate={correcting.work_date}
          timeZone={days.data.time_zone}
          employeeId={employeeId}
          onClose={() => {
            setCorrecting(null);
          }}
        />
      ) : null}
    </div>
  );
}
