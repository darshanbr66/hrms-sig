import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { routes } from "../app/App";
import { NAV_GROUPS, visibleNavGroups } from "../app/navigation";
import { FakeApi, ME, json, problem } from "../test/fakeApi";
import {
  EMPLOYEE_ME,
  HR_ADMIN_ME,
  MANAGER_ME,
  STATISTICS,
  SUPER_ADMIN_ME,
  employee,
  today,
} from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function employeeData() {
  api
    .on("GET /api/v1/me/profile", json(employee({ preferred_name: "Testy" })))
    .on("GET /api/v1/me/attendance/today", json(today()))
    .on("GET /api/v1/me/holidays", json({ items: [] }))
    .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
}

describe("dashboard", () => {
  it("greets an employee by name and clocks them in with a request key", async () => {
    employeeData();
    const clockedIn = today({
      state: "clocked_in",
      allowed_events: ["BREAK_START", "CLOCK_OUT"],
      day: { ...today().day, session_seconds: 60, first_in: "2026-10-05T03:30:00Z", open_session: true },
    });
    // The page refreshes today's state after recording, and then sees the new state.
    api.on("GET /api/v1/me/attendance/today", json(clockedIn));
    api.on("POST /api/v1/me/attendance/events", json(clockedIn));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/");
    expect(await screen.findByRole("heading", { name: /, Testy$/ })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Clock in" }));
    expect(await screen.findByRole("button", { name: "Start break" })).toBeInTheDocument();
    const [call] = api.called("POST /api/v1/me/attendance/events");
    expect(call?.body).toMatchObject({ event_type: "CLOCK_IN" });
    expect(call?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    // People figures are not shown to someone who can read only their own record.
    expect(screen.queryByRole("group", { name: "People figures" })).not.toBeInTheDocument();
  });

  it("explains an attendance conflict in plain words", async () => {
    employeeData();
    api.on(
      "POST /api/v1/me/attendance/events",
      problem("conflict", 409, {
        detail: "You are already clocked in.",
        code: "attendance.already_clocked_in",
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/");
    await user.click(await screen.findByRole("button", { name: "Clock in" }));
    expect(await screen.findByText("You are already clocked in.")).toBeInTheDocument();
  });

  it("shows HR the real figures and the organization set-up steps while nothing is set up", async () => {
    api
      .on("GET /api/v1/employees/statistics", json(STATISTICS))
      .on(
        "GET /api/v1/attendance/today",
        json({
          summary: { in_scope: 42, clocked_in_today: 30, working_now: 28, on_break: 2 },
          items: [],
          next_cursor: null,
        }),
      )
      .on("GET /api/v1/departments", json({ items: [] }))
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }))
      .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
    renderSignedIn(api, HR_ADMIN_ME, "/");
    const people = await screen.findByRole("group", { name: "People figures" });
    expect(within(people).getByText("42")).toBeInTheDocument();
    expect(within(people).getByText("On notice")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Set up your organization" })).toBeInTheDocument();
    expect(screen.getByText("Test Joiner")).toBeInTheDocument();
    const attendance = await screen.findByRole("group", { name: "Attendance today" });
    expect(within(attendance).getByText("30")).toBeInTheDocument();
    // HR without an employee record has no clock.
    expect(screen.queryByRole("button", { name: "Clock in" })).not.toBeInTheDocument();
    expect(screen.getByText(/not linked to an employee record/)).toBeInTheDocument();
  });

  it("keeps the rest of the page when one block fails, with a way to retry", async () => {
    api
      .on("GET /api/v1/employees/statistics", problem("internal-error", 500))
      .on("GET /api/v1/employees/statistics", json(STATISTICS))
      .on(
        "GET /api/v1/attendance/today",
        json({
          summary: { in_scope: 1, clocked_in_today: 0, working_now: 0, on_break: 0 },
          items: [],
          next_cursor: null,
        }),
      )
      .on("GET /api/v1/departments", json({ items: [] }))
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }))
      .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/");
    expect(await screen.findByRole("group", { name: "Attendance today" })).toBeInTheDocument();
    const retry = (await screen.findAllByRole("button", { name: "Try again" }))[0];
    expect(retry).toBeDefined();
    if (retry) await user.click(retry);
    expect(await screen.findByRole("group", { name: "People figures" })).toBeInTheDocument();
  });

  it("shows a super admin access requests and no employee data", async () => {
    api
      .on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }))
      .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
    renderSignedIn(api, SUPER_ADMIN_ME, "/");
    expect(await screen.findByText("Nothing waiting for a decision")).toBeInTheDocument();
    expect(api.called("GET /api/v1/employees/statistics")).toHaveLength(0);
    expect(api.called("GET /api/v1/me/attendance/today")).toHaveLength(0);
  });

  it("has no accessibility violations", async () => {
    employeeData();
    const { container } = renderSignedIn(api, EMPLOYEE_ME, "/");
    await screen.findByRole("button", { name: "Clock in" });
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("navigation", () => {
  it("shows each person only the groups their permissions reach", () => {
    expect(visibleNavGroups(ME).map((group) => group.label)).toEqual(["Me"]);
    expect(visibleNavGroups(MANAGER_ME).map((group) => group.label)).toEqual(["Me", "Team", "People"]);
    expect(visibleNavGroups(HR_ADMIN_ME).flatMap((group) => group.items.map((item) => item.label))).toContain(
      "Holiday calendars",
    );
    expect(
      visibleNavGroups(SUPER_ADMIN_ME).flatMap((group) => group.items.map((item) => item.label)),
    ).not.toContain("Employees");
  });

  it("leads every navigation item to a route", () => {
    const paths = new Set<string>();
    const collect = (list: typeof routes) => {
      for (const route of list) {
        if (route.path) paths.add(route.path);
        if (route.children) collect(route.children);
      }
    };
    collect(routes);
    for (const item of NAV_GROUPS.flatMap((group) => group.items)) {
      expect(paths, item.to).toContain(item.to);
    }
  });

  it("opens the menu drawer and follows a link", async () => {
    employeeData();
    api.on("GET /api/v1/me/attendance/days", json({ time_zone: "Asia/Kolkata", items: [] }));
    const user = userEvent.setup();
    const { router } = renderSignedIn(api, EMPLOYEE_ME, "/");
    await user.click(await screen.findByRole("button", { name: "Open navigation" }));
    const drawer = await screen.findByRole("navigation", { name: "Main (menu)" });
    await user.click(within(drawer).getByRole("link", { name: "My attendance" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/attendance");
    });
    expect(screen.queryByRole("navigation", { name: "Main (menu)" })).not.toBeInTheDocument();
  });
});
