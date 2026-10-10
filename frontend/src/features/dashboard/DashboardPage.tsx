/**
 * The home page (ui-ux-guidelines.md §2, Home): what the person needs to do now, then what is
 * coming up, then the figures their role works with. Every block appears only for people who
 * hold a permission for its data, loads on its own, and fails on its own with a retry, so one
 * unavailable source never blanks the page. Every figure comes from the API; nothing is
 * estimated or invented here.
 */
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Building2,
  CalendarDays,
  CalendarRange,
  Cake,
  KeyRound,
  ShieldAlert,
  UserPlus,
  Users,
  UsersRound,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { canSee } from "../../app/navigation";
import { hasPermission, useMe, type Me } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { ButtonLink } from "../../design-system/Button";
import { EmptyState } from "../../design-system/EmptyState";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { Stat, StatGrid } from "../../design-system/Stat";
import { client, unwrap } from "../../lib/api";
import { addDays, formatDate, formatLongToday, formatRelative, localIsoDate, plural } from "../../lib/format";
import { ClockCard } from "../attendance/ClockCard";
import { ATTENDANCE_KEY } from "../attendance/queries";
import { PEOPLE_KEY, useStatistics } from "../employees/queries";
import { HOLIDAYS_KEY } from "../holidays/MyHolidaysPage";
import { useDepartments, useDesignations, useLocations } from "../organization/queries";

const READS_PEOPLE = ["employee.profile.read.team", "employee.profile.read.all"];
const READS_TEAM_ATTENDANCE = ["attendance.read.team", "attendance.read.all"];

function greeting(now: Date): string {
  const hour = now.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function DashboardPage() {
  const me = useMe();
  const data = me.data;
  const isEmployee = Boolean(data?.employee_id);
  const profile = useQuery({
    queryKey: [...PEOPLE_KEY, "me"],
    queryFn: () => unwrap(client.GET("/api/v1/me/profile")),
    enabled: isEmployee && hasPermission(data, "employee.profile.read.self"),
  });
  const firstName = profile.data ? (profile.data.preferred_name ?? profile.data.legal_first_name) : null;
  const now = new Date();
  const readsPeople = canSee(data, { anyOf: READS_PEOPLE });
  const readsTeam = canSee(data, { anyOf: READS_TEAM_ATTENDANCE });

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-[1.375rem] font-semibold text-neutral-900">
          {greeting(now)}
          {firstName ? `, ${firstName}` : ""}
        </h1>
        <p className="mt-1 text-sm text-neutral-600">{formatLongToday(now)}</p>
      </header>

      {hasPermission(data, "org.manage") ? <SetupChecklist /> : null}

      {!isEmployee ? (
        <Banner tone="info">
          Your account is not linked to an employee record, so attendance, holidays and a profile are not
          available to you. Everything you can manage is in the navigation.
        </Banner>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        {hasPermission(data, "attendance.clock.self") ? (
          <div className="lg:col-span-2">
            <ClockCard compact />
          </div>
        ) : null}
        {isEmployee && hasPermission(data, "holiday.read") ? <UpcomingHolidays /> : null}
      </div>

      {readsPeople ? <Workforce /> : null}
      {readsTeam ? <TeamToday /> : null}

      <div className="grid gap-6 lg:grid-cols-2">
        {readsPeople ? <PeopleMoments /> : null}
        {hasPermission(data, "role.read") ? <PendingAccessRequests /> : null}
        {hasPermission(data, "auth.session.read.self") ? <RecentSignIns /> : null}
        <QuickActions me={data} />
      </div>
    </div>
  );
}

function Loading({
  query,
  label,
  fallback,
}: {
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  label: string;
  fallback: string;
}) {
  if (query.isPending) return <Skeleton label={label} />;
  if (query.isError)
    return <ProblemMessage error={query.error} fallback={fallback} onRetry={() => void query.refetch()} />;
  return null;
}

/** For an organization with nothing set up yet: the next steps, in order. */
function SetupChecklist() {
  const departments = useDepartments();
  const designations = useDesignations();
  const locations = useLocations();
  if (!departments.isSuccess || !designations.isSuccess || !locations.isSuccess) return null;
  const steps = [
    { done: locations.data.items.length > 0, text: "Add your locations, with their time zones" },
    { done: departments.data.items.length > 0, text: "Add your departments" },
    { done: designations.data.items.length > 0, text: "Add your designations" },
  ];
  if (steps.every((step) => step.done)) return null;
  return (
    <Panel
      title="Set up your organization"
      description="Employee records are placed in a department, designation and location, so those come first."
      action={
        <ButtonLink to="/organization" size="sm">
          Open organization
          <ArrowRight aria-hidden className="size-4" />
        </ButtonLink>
      }
    >
      <ol className="flex flex-col gap-2 text-sm">
        {steps.map((step, index) => (
          <li key={step.text} className="flex items-center gap-3">
            <span
              className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                step.done ? "bg-success-50 text-success-700" : "bg-neutral-100 text-neutral-700"
              }`}
            >
              {index + 1}
            </span>
            <span className={step.done ? "text-neutral-500 line-through" : "text-neutral-900"}>
              {step.text}
            </span>
            <span className="sr-only">{step.done ? "(done)" : "(to do)"}</span>
          </li>
        ))}
      </ol>
    </Panel>
  );
}

function UpcomingHolidays() {
  const from = localIsoDate();
  const to = addDays(from, 120);
  const holidays = useQuery({
    queryKey: [...HOLIDAYS_KEY, "upcoming", from],
    queryFn: () => unwrap(client.GET("/api/v1/me/holidays", { params: { query: { from, to } } })),
  });
  const items = holidays.data?.items.slice(0, 4) ?? [];
  return (
    <Panel
      title="Upcoming holidays"
      action={
        <Link className="text-sm text-accent-700 hover:underline" to="/holidays">
          See all
        </Link>
      }
    >
      <Loading query={holidays} label="Loading holidays" fallback="We couldn't load the holidays." />
      {holidays.isSuccess && items.length === 0 ? (
        <EmptyState icon={CalendarDays} title="No holidays in the next four months">
          Holidays appear once HR publishes the calendar for your location.
        </EmptyState>
      ) : null}
      {items.length > 0 ? (
        <ul className="flex flex-col gap-3">
          {items.map((item) => (
            <li key={item.date} className="flex items-start justify-between gap-3">
              <span className="text-sm font-semibold text-neutral-900">{item.name}</span>
              <span className="shrink-0 text-sm text-neutral-600">
                {formatDate(item.date, { year: false })}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </Panel>
  );
}

function Workforce() {
  const stats = useStatistics(true);
  return (
    <section aria-labelledby="workforce-heading" className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 id="workforce-heading" className="text-base font-semibold text-neutral-900">
          People
        </h2>
        <Link className="text-sm text-accent-700 hover:underline" to="/employees">
          Employees
        </Link>
      </div>
      <Loading query={stats} label="Loading people figures" fallback="We couldn't load the people figures." />
      {stats.isSuccess ? (
        stats.data.employed === 0 && stats.data.pre_joining === 0 ? (
          <EmptyState icon={Users} title="No employee records yet">
            Figures appear here once employee records exist.
          </EmptyState>
        ) : (
          <StatGrid label="People figures">
            <Stat label="Employed today" value={stats.data.employed} to="/employees" />
            <Stat label="On notice" value={stats.data.on_notice} to="/employees?status=on_notice" />
            <Stat
              label="Joining soon"
              value={stats.data.pre_joining}
              hint="Records before their joining date"
              to="/employees?status=pre_joining"
            />
            <Stat label="Joined in the last 30 days" value={stats.data.joined_last_30_days} />
          </StatGrid>
        )
      ) : null}
    </section>
  );
}

function TeamToday() {
  const team = useQuery({
    queryKey: [...ATTENDANCE_KEY, "team-summary"],
    queryFn: () => unwrap(client.GET("/api/v1/attendance/today", { params: { query: { limit: 1 } } })),
    refetchInterval: 120_000,
  });
  const summary = team.data?.summary;
  return (
    <section aria-labelledby="team-today-heading" className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 id="team-today-heading" className="text-base font-semibold text-neutral-900">
          Attendance today
        </h2>
        <Link className="text-sm text-accent-700 hover:underline" to="/team/attendance">
          Team attendance
        </Link>
      </div>
      <Loading
        query={team}
        label="Loading today's attendance"
        fallback="We couldn't load today's attendance."
      />
      {summary ? (
        <>
          <StatGrid label="Attendance today">
            <Stat
              label="Clocked in today"
              value={summary.clocked_in_today}
              hint={`of ${plural(summary.in_scope, "person", "people")}`}
              to="/team/attendance"
            />
            <Stat label="Working now" value={summary.working_now} />
            <Stat label="On a break" value={summary.on_break} />
          </StatGrid>
          <p className="text-xs text-neutral-600">
            Clocking in is not the same as being present for the day: that depends on shifts, weekly offs and
            leave, which are not configured yet.
          </p>
        </>
      ) : null}
    </section>
  );
}

function PeopleMoments() {
  const stats = useStatistics(true);
  const anniversaries = stats.data?.work_anniversaries ?? [];
  const starting = stats.data?.starting_soon ?? [];
  return (
    <Panel
      title="Coming up"
      description="Work anniversaries in the next two weeks and people joining in the next month."
    >
      <Loading query={stats} label="Loading what is coming up" fallback="We couldn't load this." />
      {stats.isSuccess && anniversaries.length === 0 && starting.length === 0 ? (
        <EmptyState icon={Cake} title="Nothing coming up" />
      ) : null}
      <ul className="flex flex-col gap-3">
        {starting.map((item) => (
          <MomentRow
            key={`start-${item.employee.id}`}
            id={item.employee.id}
            name={item.employee.name}
            detail={`Joins ${formatDate(item.date_of_joining, { year: false })}`}
          />
        ))}
        {anniversaries.map((item) => (
          <MomentRow
            key={`anniversary-${item.employee.id}`}
            id={item.employee.id}
            name={item.employee.name}
            detail={`${plural(item.years, "year", "years")} on ${formatDate(item.date, { year: false })}`}
          />
        ))}
      </ul>
    </Panel>
  );
}

function MomentRow({ id, name, detail }: { id: string; name: string; detail: string }) {
  return (
    <li className="flex items-center justify-between gap-3 text-sm">
      <Link className="font-semibold text-neutral-900 hover:underline" to={`/employees/${id}`}>
        {name}
      </Link>
      <span className="text-neutral-600">{detail}</span>
    </li>
  );
}

function PendingAccessRequests() {
  const requests = useQuery({
    queryKey: ["access", "grant-requests", "pending"],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/role-grant-requests", { params: { query: { status: "pending", limit: 5 } } }),
      ),
  });
  const items = requests.data?.items ?? [];
  return (
    <Panel
      title="Access requests waiting"
      action={
        <Link className="text-sm text-accent-700 hover:underline" to="/admin/access-requests">
          Access requests
        </Link>
      }
    >
      <Loading
        query={requests}
        label="Loading access requests"
        fallback="We couldn't load the access requests."
      />
      {requests.isSuccess && items.length === 0 ? (
        <EmptyState icon={ShieldAlert} title="Nothing waiting for a decision" />
      ) : null}
      <ul className="flex flex-col gap-3">
        {items.map((item) => (
          <li key={item.id} className="text-sm">
            <p className="font-semibold text-neutral-900">
              {item.subject.email ?? "An account"}: {item.role_key.replace(/_/g, " ")}
            </p>
            <p className="text-neutral-600">
              Asked {formatRelative(item.requested_at)}
              {item.decide_by
                ? `, decide by ${formatDate(item.decide_by.slice(0, 10), { year: false })}`
                : ""}
            </p>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function RecentSignIns() {
  const history = useQuery({
    queryKey: ["identity", "login-history", "recent"],
    queryFn: () => unwrap(client.GET("/api/v1/me/login-history", { params: { query: { limit: 3 } } })),
  });
  const items = history.data?.items ?? [];
  const labels = {
    "login.succeeded": "Signed in",
    "login.failed": "Failed sign-in",
    "account.locked": "Account locked",
  };
  return (
    <Panel
      title="Your recent sign-ins"
      description="If you don't recognize one, change your password and sign out other sessions."
      action={
        <Link className="text-sm text-accent-700 hover:underline" to="/account/security">
          Account security
        </Link>
      }
    >
      <Loading query={history} label="Loading your sign-ins" fallback="We couldn't load your sign-ins." />
      {history.isSuccess && items.length === 0 ? (
        <p className="text-sm text-neutral-600">No sign-ins recorded yet.</p>
      ) : null}
      <ul className="flex flex-col gap-3">
        {items.map((item) => (
          <li key={item.occurred_at} className="flex items-start justify-between gap-3 text-sm">
            <span>
              <span
                className={`font-semibold ${item.event_type === "login.succeeded" ? "text-neutral-900" : "text-danger-700"}`}
              >
                {labels[item.event_type]}
              </span>
              <span className="block truncate text-xs text-neutral-600">{item.ip ?? "Unknown address"}</span>
            </span>
            <span className="shrink-0 text-neutral-600" title={new Date(item.occurred_at).toLocaleString()}>
              {formatRelative(item.occurred_at)}
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function QuickActions({ me }: { me: Me | null | undefined }) {
  const actions: { to: string; label: string; icon: typeof Users; show: boolean }[] = [
    {
      to: "/employees/new",
      label: "Add an employee",
      icon: UserPlus,
      show: hasPermission(me, "employee.create"),
    },
    {
      to: "/organization",
      label: "Manage organization",
      icon: Building2,
      show: hasPermission(me, "org.manage"),
    },
    {
      to: "/setup/holiday-calendars",
      label: "Holiday calendars",
      icon: CalendarRange,
      show: hasPermission(me, "holiday.manage"),
    },
    {
      to: "/team/attendance",
      label: "Team attendance",
      icon: UsersRound,
      show: canSee(me, { anyOf: READS_TEAM_ATTENDANCE }),
    },
    {
      to: "/admin/users",
      label: "Invite or manage users",
      icon: KeyRound,
      show: hasPermission(me, "user.read.all"),
    },
    {
      to: "/directory",
      label: "Find a colleague",
      icon: Users,
      show: hasPermission(me, "employee.directory.read"),
    },
  ].filter((action) => action.show);
  if (actions.length === 0) return null;
  return (
    <Panel title="Shortcuts">
      <ul className="grid gap-2 sm:grid-cols-2">
        {actions.map((action) => (
          <li key={action.to}>
            <ShortcutLink
              to={action.to}
              icon={<action.icon aria-hidden className="size-4 text-accent-700" />}
            >
              {action.label}
            </ShortcutLink>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function ShortcutLink({ to, icon, children }: { to: string; icon: ReactNode; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="flex items-center gap-2 rounded-[var(--radius-control)] border border-neutral-200 px-3 py-2 text-sm text-neutral-800 hover:border-accent-600 hover:bg-accent-50"
    >
      {icon}
      {children}
    </Link>
  );
}
