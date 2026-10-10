import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { ProblemMessage } from "../design-system/ProblemMessage";
import { HOME } from "../lib/navigation";
import { Skeleton } from "../design-system/Skeleton";
import { AccessDenied } from "./pages";
import { hasPermission, useMe } from "./session";

export const ENROLMENT_PATH = "/account/mfa-enrolment";

/** A signed-in session is required. An enrolment-only session reaches only MFA enrolment. */
export function RequireSession({
  children,
  enrolment = false,
}: {
  children: ReactNode;
  enrolment?: boolean;
}) {
  const me = useMe();
  const location = useLocation();
  if (me.isPending) {
    return <Skeleton label="Loading your account" />;
  }
  if (me.isError) {
    return <ProblemMessage error={me.error} fallback="We couldn't load your account. Try again." />;
  }
  if (me.data === null) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/sign-in?next=${next}`} replace />;
  }
  const enrolmentOnly = me.data.session_scope === "mfa_enrolment";
  if (enrolmentOnly && !enrolment) {
    return <Navigate to={ENROLMENT_PATH} replace />;
  }
  if (!enrolmentOnly && enrolment) {
    return <Navigate to={HOME} replace />;
  }
  return children;
}

/** Pages a user cannot use are not linked; reached by URL, they say so. */
export function RequirePermission({
  permission,
  anyOf,
  children,
}: {
  permission?: string;
  anyOf?: readonly string[];
  children: ReactNode;
}) {
  const me = useMe();
  const needed = anyOf ?? (permission ? [permission] : []);
  return needed.some((key) => hasPermission(me.data, key)) ? children : <AccessDenied />;
}
