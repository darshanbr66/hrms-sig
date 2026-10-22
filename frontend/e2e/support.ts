/**
 * A fake API for browser tests: an HR admin session and obviously fake records. Every write is
 * refused, so a test can only look; nothing reaches a database.
 */
import type { Route } from "@playwright/test";

import roles from "../src/test/role-permissions.json" with { type: "json" };

export const HR_ADMIN = {
  user_id: "01900000-0000-7000-8000-000000000001",
  email: "hr.admin@dev.example",
  employee_id: null,
  session_id: "01900000-0000-7000-8000-000000000002",
  session_scope: "full",
  step_up_expires_at: null,
  roles: ["hr_admin"],
  permissions: [...new Set([...roles.account_baseline, ...roles.hr_admin])],
};

const STATISTICS = {
  employed: 0,
  active: 0,
  on_notice: 0,
  pre_joining: 0,
  future_joiners: 0,
  former: 0,
  joined_last_30_days: 0,
  starting_soon: [],
  work_anniversaries: [],
  recent_joiners: [],
  by_department: [],
  by_designation: [],
  by_location: [],
};

const CALENDAR = {
  id: "c-1",
  name: "Test calendar",
  year: 2031,
  status: "active",
  version: 1,
  holidays: [
    { id: "h-1", date: "2031-01-01", name: "Test holiday", is_optional: false, kind: "holiday" },
    { id: "h-2", date: "2031-02-01", name: "Test optional holiday", is_optional: true, kind: "holiday" },
    { id: "h-3", date: "2031-03-08", name: "Test working Saturday", is_optional: false, kind: "working_day" },
  ],
  locations: [{ id: "l-1", name: "Test office" }],
  updated_at: "2031-01-01T00:00:00Z",
};

const CALENDARS = {
  items: [
    {
      id: "c-1",
      name: "Test calendar",
      year: 2031,
      status: "active",
      holiday_count: 2,
      working_day_count: 1,
      location_count: 1,
    },
    {
      id: "c-2",
      name: "Test old calendar",
      year: 2030,
      status: "archived",
      holiday_count: 0,
      working_day_count: 0,
      location_count: 0,
    },
  ],
};

const LEAVE_TYPES = {
  items: [
    {
      id: "t-1",
      code: "TL",
      name: "Test leave",
      description: null,
      is_paid: true,
      requires_approval: true,
      allows_half_day: false,
      counts_weekly_offs: false,
      counts_holidays: false,
      status: "active",
      version: 1,
      created_at: "2031-01-01T00:00:00Z",
      updated_at: "2031-01-01T00:00:00Z",
    },
  ],
};

const SETUP = {
  date: "2026-10-05",
  active_shifts: 0,
  policies_in_effect: 0,
  organization_policy: false,
  employees_in_scope: 0,
  without_shift: [],
  without_policy: [],
};

export async function fakeApi(route: Route) {
  const url = new URL(route.request().url());
  const reply = (status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  if (route.request().method() !== "GET") {
    return reply(403, { type: "forbidden", title: "You don't have access to this.", status: 403 });
  }
  switch (url.pathname) {
    case "/api/health/ready":
      return reply(200, { status: "ok" });
    case "/api/v1/me":
      return reply(200, HR_ADMIN);
    case "/api/v1/employees/statistics":
      return reply(200, STATISTICS);
    case "/api/v1/holiday-calendars":
      return reply(200, CALENDARS);
    case "/api/v1/holiday-calendars/c-1":
      return reply(200, CALENDAR);
    case "/api/v1/leave-types":
      return reply(200, LEAVE_TYPES);
    case "/api/v1/attendance/setup":
      return reply(200, SETUP);
    case "/api/v1/me/notifications/unread-count":
      return reply(200, { unread_count: 0 });
    default:
      return reply(200, { items: [], next_cursor: null });
  }
}

/** The same fake API for a browser that holds no session: `/api/v1/me` is unauthenticated and the
 * refresh is refused, as after sign-out, expiry or revocation. */
export async function signedOutApi(route: Route) {
  const path = new URL(route.request().url()).pathname;
  if (path === "/api/v1/me" || path === "/api/v1/auth/refresh") {
    return route.fulfill({
      status: 401,
      contentType: "application/problem+json",
      body: JSON.stringify({ type: "/problems/unauthenticated", title: "Sign in to continue.", status: 401 }),
    });
  }
  return fakeApi(route);
}
