import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import {
  Navigate,
  Outlet,
  RouterProvider,
  createBrowserRouter,
  useLocation,
  useNavigate,
  type RouteObject,
} from "react-router";

import { SignInPage } from "../features/auth/SignInPage";
import { VerifyPage } from "../features/auth/VerifyPage";
import { Skeleton } from "../design-system/Skeleton";
import { onSessionEnded } from "../lib/api";
import { HOME } from "../lib/navigation";
import { AppShell } from "./AppShell";
import { ENROLMENT_PATH, RequireSession } from "./guards";
import { NotFound } from "./pages";
import { StepUpProvider } from "./StepUp";

// Feature routes load on demand (ui-ux-guidelines.md §10); sign-in stays in the first bundle.
const SecurityPage = lazy(() =>
  import("../features/account/SecurityPage").then((m) => ({ default: m.SecurityPage })),
);
const MfaEnrolmentPage = lazy(() =>
  import("../features/account/MfaEnrolmentPage").then((m) => ({ default: m.MfaEnrolmentPage })),
);
const InvitePage = lazy(() => import("../features/auth/InvitePage").then((m) => ({ default: m.InvitePage })));

function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Skeleton label="Loading" />}>{children}</Suspense>;
}

/** When a refresh fails the session is over: drop cached data and return to sign-in. */
function SessionEndedRedirect() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(
    () =>
      onSessionEnded(() => {
        if (location.pathname.startsWith("/sign-in") || location.pathname.startsWith("/invite/")) return;
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
      { path: "/sign-in", element: <SignInPage /> },
      { path: "/sign-in/verify", element: <VerifyPage /> },
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
          <StepUpProvider>
            <AppShell />
          </StepUpProvider>
        ),
        children: [
          { path: "/", element: <Navigate to={HOME} replace /> },
          {
            path: HOME,
            element: (
              <RequireSession>
                <Page>
                  <SecurityPage />
                </Page>
              </RequireSession>
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
