import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import {
  Outlet,
  RouterProvider,
  createBrowserRouter,
  useLocation,
  useNavigate,
  type RouteObject,
} from "react-router";

import { Skeleton } from "../design-system/Skeleton";
import { ToastProvider } from "../design-system/Toast";
import { onSessionEnded } from "../lib/api";
import { HOME } from "../lib/navigation";
import { AppShell } from "./AppShell";
import { ENROLMENT_PATH, RequirePermission, RequireSession } from "./guards";
import { NotFound } from "./pages";
import { StepUpProvider } from "./StepUp";

// Every page loads on demand (ui-ux-guidelines.md §10), sign-in included, so the first bundle
// holds only the shell that every page needs.
const SignInPage = lazy(() => import("../features/auth/SignInPage").then((m) => ({ default: m.SignInPage })));
const VerifyPage = lazy(() => import("../features/auth/VerifyPage").then((m) => ({ default: m.VerifyPage })));
const SecurityPage = lazy(() =>
  import("../features/account/SecurityPage").then((m) => ({ default: m.SecurityPage })),
);
const MfaEnrolmentPage = lazy(() =>
  import("../features/account/MfaEnrolmentPage").then((m) => ({ default: m.MfaEnrolmentPage })),
);
const PasswordResetRequestPage = lazy(() =>
  import("../features/auth/PasswordResetRequestPage").then((m) => ({ default: m.PasswordResetRequestPage })),
);
const PasswordResetPage = lazy(() =>
  import("../features/auth/PasswordResetPage").then((m) => ({ default: m.PasswordResetPage })),
);
const SettingsPage = lazy(() =>
  import("../features/settings/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);
const MfaReenrolmentPage = lazy(() =>
  import("../features/auth/MfaReenrolmentPage").then((m) => ({ default: m.MfaReenrolmentPage })),
);
const AccessRequestsPage = lazy(() =>
  import("../features/admin/AccessRequestsPage").then((m) => ({ default: m.AccessRequestsPage })),
);
const InvitePage = lazy(() => import("../features/auth/InvitePage").then((m) => ({ default: m.InvitePage })));

const DashboardPage = lazy(() =>
  import("../features/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const MyAttendancePage = lazy(() =>
  import("../features/attendance/MyAttendancePage").then((m) => ({ default: m.MyAttendancePage })),
);
const TeamAttendancePage = lazy(() =>
  import("../features/attendance/TeamAttendancePage").then((m) => ({ default: m.TeamAttendancePage })),
);
const MyHolidaysPage = lazy(() =>
  import("../features/holidays/MyHolidaysPage").then((m) => ({ default: m.MyHolidaysPage })),
);
const HolidayCalendarsPage = lazy(() =>
  import("../features/holidays/HolidayCalendarsPage").then((m) => ({ default: m.HolidayCalendarsPage })),
);
const HolidayCalendarPage = lazy(() =>
  import("../features/holidays/HolidayCalendarPage").then((m) => ({ default: m.HolidayCalendarPage })),
);
const MyProfilePage = lazy(() =>
  import("../features/employees/MyProfilePage").then((m) => ({ default: m.MyProfilePage })),
);
const DirectoryPage = lazy(() =>
  import("../features/employees/DirectoryPage").then((m) => ({ default: m.DirectoryPage })),
);
const EmployeesPage = lazy(() =>
  import("../features/employees/EmployeesPage").then((m) => ({ default: m.EmployeesPage })),
);
const NewEmployeePage = lazy(() =>
  import("../features/employees/NewEmployeePage").then((m) => ({ default: m.NewEmployeePage })),
);
const EmployeePage = lazy(() =>
  import("../features/employees/EmployeePage").then((m) => ({ default: m.EmployeePage })),
);
const OrganizationPage = lazy(() =>
  import("../features/organization/OrganizationPage").then((m) => ({ default: m.OrganizationPage })),
);
const UsersPage = lazy(() => import("../features/admin/UsersPage").then((m) => ({ default: m.UsersPage })));
const UserPage = lazy(() => import("../features/admin/UserPage").then((m) => ({ default: m.UserPage })));
const RolesPage = lazy(() => import("../features/admin/RolesPage").then((m) => ({ default: m.RolesPage })));
const SecurityEventsPage = lazy(() =>
  import("../features/admin/SecurityEventsPage").then((m) => ({ default: m.SecurityEventsPage })),
);
const AuditLogPage = lazy(() =>
  import("../features/admin/AuditLogPage").then((m) => ({ default: m.AuditLogPage })),
);

function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Skeleton label="Loading" />}>{children}</Suspense>;
}

/** A signed-in page, optionally needing any one of some permissions (cosmetic: the API decides). */
function Protected({ anyOf, children }: { anyOf?: readonly string[]; children: ReactNode }) {
  const page = <Page>{children}</Page>;
  return (
    <RequireSession>
      {anyOf ? <RequirePermission anyOf={anyOf}>{page}</RequirePermission> : page}
    </RequireSession>
  );
}

/** When a refresh fails the session is over: drop cached data and return to sign-in. */
function SessionEndedRedirect() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(
    () =>
      onSessionEnded(() => {
        const publicPaths = ["/sign-in", "/invite/", "/mfa-reenrolment/", "/password-reset"];
        if (publicPaths.some((prefix) => location.pathname.startsWith(prefix))) return;
        const next = encodeURIComponent(location.pathname + location.search);
        void navigate(`/sign-in?expired=1&next=${next}`, { replace: true });
      }),
    [location, navigate],
  );
  return <Outlet />;
}

export const routes: RouteObject[] = [
  {
    element: <SessionEndedRedirect />,
    children: [
      {
        path: "/sign-in",
        element: (
          <Page>
            <SignInPage />
          </Page>
        ),
      },
      {
        path: "/sign-in/verify",
        element: (
          <Page>
            <VerifyPage />
          </Page>
        ),
      },
      {
        path: "/password-reset",
        element: (
          <Page>
            <PasswordResetRequestPage />
          </Page>
        ),
      },
      {
        path: "/password-reset/:token",
        element: (
          <Page>
            <PasswordResetPage />
          </Page>
        ),
      },
      {
        path: "/mfa-reenrolment/:token",
        element: (
          <Page>
            <MfaReenrolmentPage />
          </Page>
        ),
      },
      {
        path: "/invite/:token",
        element: (
          <Page>
            <InvitePage />
          </Page>
        ),
      },
      {
        element: (
          <ToastProvider>
            <StepUpProvider>
              <AppShell />
            </StepUpProvider>
          </ToastProvider>
        ),
        children: [
          {
            path: HOME,
            element: (
              <Protected>
                <DashboardPage />
              </Protected>
            ),
          },
          {
            path: "/account/security",
            element: (
              <Protected>
                <SecurityPage />
              </Protected>
            ),
          },
          {
            path: "/attendance",
            element: (
              <Protected anyOf={["attendance.clock.self"]}>
                <MyAttendancePage />
              </Protected>
            ),
          },
          {
            path: "/team/attendance",
            element: (
              <Protected anyOf={["attendance.read.team", "attendance.read.all"]}>
                <TeamAttendancePage />
              </Protected>
            ),
          },
          {
            path: "/holidays",
            element: (
              <Protected anyOf={["holiday.read"]}>
                <MyHolidaysPage />
              </Protected>
            ),
          },
          {
            path: "/profile",
            element: (
              <Protected anyOf={["employee.profile.read.self"]}>
                <MyProfilePage />
              </Protected>
            ),
          },
          {
            path: "/directory",
            element: (
              <Protected anyOf={["employee.directory.read"]}>
                <DirectoryPage />
              </Protected>
            ),
          },
          {
            path: "/employees",
            element: (
              <Protected anyOf={["employee.profile.read.team", "employee.profile.read.all"]}>
                <EmployeesPage />
              </Protected>
            ),
          },
          {
            path: "/employees/new",
            element: (
              <Protected anyOf={["employee.create"]}>
                <NewEmployeePage />
              </Protected>
            ),
          },
          {
            path: "/employees/:employeeId",
            element: (
              <Protected anyOf={["employee.profile.read.team", "employee.profile.read.all"]}>
                <EmployeePage />
              </Protected>
            ),
          },
          {
            path: "/organization",
            element: (
              <Protected anyOf={["org.read"]}>
                <OrganizationPage />
              </Protected>
            ),
          },
          {
            path: "/setup/holiday-calendars",
            element: (
              <Protected anyOf={["holiday.manage"]}>
                <HolidayCalendarsPage />
              </Protected>
            ),
          },
          {
            path: "/setup/holiday-calendars/:calendarId",
            element: (
              <Protected anyOf={["holiday.manage"]}>
                <HolidayCalendarPage />
              </Protected>
            ),
          },
          {
            path: "/admin/users",
            element: (
              <Protected anyOf={["user.read.all"]}>
                <UsersPage />
              </Protected>
            ),
          },
          {
            path: "/admin/users/:userId",
            element: (
              <Protected anyOf={["user.read.all"]}>
                <UserPage />
              </Protected>
            ),
          },
          {
            path: "/admin/roles",
            element: (
              <Protected anyOf={["role.read"]}>
                <RolesPage />
              </Protected>
            ),
          },
          {
            path: "/admin/access-requests",
            element: (
              <Protected anyOf={["role.read"]}>
                <AccessRequestsPage />
              </Protected>
            ),
          },
          {
            path: "/admin/security-events",
            element: (
              <Protected anyOf={["security.event.read"]}>
                <SecurityEventsPage />
              </Protected>
            ),
          },
          {
            path: "/admin/audit-log",
            element: (
              <Protected anyOf={["audit.read"]}>
                <AuditLogPage />
              </Protected>
            ),
          },
          {
            path: "/settings",
            element: (
              <Protected anyOf={["settings.read"]}>
                <SettingsPage />
              </Protected>
            ),
          },
          {
            path: ENROLMENT_PATH,
            element: (
              <RequireSession enrolment>
                <Page>
                  <MfaEnrolmentPage />
                </Page>
              </RequireSession>
            ),
          },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
];

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
}

export function App() {
  const [queryClient] = useState(createQueryClient);
  const [router] = useState(() => createBrowserRouter(routes));
  useEffect(
    () =>
      onSessionEnded(() => {
        queryClient.clear();
      }),
    [queryClient],
  );
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
