import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { FakeApi, json, problem } from "../test/fakeApi";
import { EMPLOYEE_ME, HR_ADMIN_ME, MANAGER_ME, SUPER_ADMIN_ME, today } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const USER_ID = "01900000-0000-7000-8000-0000000000a1";

function account(overrides: Record<string, unknown> = {}) {
  return {
    id: USER_ID,
    email: "test.account@dev.example",
    status: "active",
    employee_id: null,
    locked: false,
    mfa_reenrolment_required: false,
    last_login_at: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("attendance", () => {
  it("shows recorded days with the times and the time between them", async () => {
    api.on("GET /api/v1/me/attendance/today", json(today())).on(
      "GET /api/v1/me/attendance/days",
      json({
        time_zone: "Asia/Kolkata",
        items: [
          {
            work_date: "2026-10-02",
            first_in: "2026-10-02T03:30:00Z",
            last_out: "2026-10-02T12:30:00Z",
            session_seconds: 32400,
            break_seconds: 2700,
            open_session: false,
            on_break: false,
            missing_break_end: false,
            events: [],
          },
        ],
      }),
    );
    const { container } = renderSignedIn(api, EMPLOYEE_ME, "/attendance");
    const table = await screen.findByRole("table", { name: "Your attendance days" });
    expect(within(table).getByText("9h 00m")).toBeInTheDocument();
    expect(within(table).getByText("45m")).toBeInTheDocument();
    expect(within(table).getByText(/^09:00/)).toBeInTheDocument();
    expect(
      screen.getByText(/Day status \(present, late, half day\) is shown once HR configures/),
    ).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("warns about a session left open from an earlier day", async () => {
    api.on(
      "GET /api/v1/me/attendance/today",
      json(
        today({
          state: "clocked_in",
          allowed_events: ["BREAK_START", "CLOCK_OUT"],
          open_since: "2026-10-04",
        }),
      ),
    );
    api.on("GET /api/v1/me/attendance/days", json({ time_zone: "Asia/Kolkata", items: [] }));
    renderSignedIn(api, EMPLOYEE_ME, "/attendance");
    expect(await screen.findByText(/have not clocked out/)).toBeInTheDocument();
  });

  it("shows a manager the team's day with counts over the whole team", async () => {
    api.on(
      "GET /api/v1/attendance/today",
      json({
        summary: { in_scope: 3, clocked_in_today: 2, working_now: 1, on_break: 1 },
        items: [
          {
            employee: {
              id: "e-1",
              name: "Test Report",
              employee_code: "TEST-1",
              department: "Test department",
              designation: "Test designation",
              location: "Test location",
            },
            state: "on_break",
            work_date: "2026-10-05",
            first_in: "2026-10-05T03:30:00Z",
            last_event_at: "2026-10-05T07:30:00Z",
            session_seconds: 14400,
            break_seconds: 600,
          },
        ],
        next_cursor: null,
      }),
    );
    renderSignedIn(api, MANAGER_ME, "/team/attendance");
    const counts = await screen.findByRole("group", { name: "Today's attendance" });
    expect(within(counts).getByText("Clocked in today").nextSibling).toHaveTextContent("2");
    const table = screen.getByRole("table", { name: "Team attendance today" });
    expect(within(table).getByRole("link", { name: "Test Report" })).toBeInTheDocument();
    expect(within(table).getByText("On a break")).toBeInTheDocument();
  });

  it("is not reachable without a team or HR attendance scope", async () => {
    renderSignedIn(api, EMPLOYEE_ME, "/team/attendance");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });
});

describe("holidays", () => {
  it("lists the location's holidays and marks the next one", async () => {
    api.on(
      "GET /api/v1/me/holidays",
      json({
        items: [
          { date: "2000-01-26", name: "Test past day", is_optional: false, calendar: "Test calendar" },
          { date: "2999-08-15", name: "Test future day", is_optional: true, calendar: "Test calendar" },
        ],
      }),
    );
    renderSignedIn(api, EMPLOYEE_ME, "/holidays");
    expect(await screen.findByText("Test future day")).toBeInTheDocument();
    expect(screen.getByText("Next")).toBeInTheDocument();
    expect(screen.getByText("Optional")).toBeInTheDocument();
    expect(screen.getByText("Calendar: Test calendar")).toBeInTheDocument();
  });

  it("lets HR admins add a holiday to a calendar with the version they saw", async () => {
    const calendar = {
      id: "c-1",
      name: "Test calendar",
      year: 2031,
      status: "active",
      version: 4,
      holidays: [],
      locations: [],
      updated_at: "2031-01-01T00:00:00Z",
    };
    const updated = {
      ...calendar,
      version: 5,
      holidays: [{ id: "h-1", date: "2031-01-01", name: "Test holiday", is_optional: false }],
    };
    api
      .on("GET /api/v1/holiday-calendars/c-1", json(calendar))
      .on("GET /api/v1/holiday-calendars/c-1", json(updated))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("POST /api/v1/holiday-calendars/c-1/holidays", json(updated, 201));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/setup/holiday-calendars/c-1");
    await user.click(await screen.findByRole("button", { name: "Add holiday" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Date"), "2031-01-01");
    await user.type(within(dialog).getByLabelText("Name"), "Test holiday");
    await user.click(within(dialog).getByRole("button", { name: "Add holiday" }));
    expect(await screen.findByText("Holiday added")).toBeInTheDocument();
    const [call] = api.called("POST /api/v1/holiday-calendars/c-1/holidays");
    expect(call?.headers.get("If-Match")).toBe('"4"');
    expect(await screen.findByRole("table", { name: "Holidays" })).toHaveTextContent("Test holiday");
  });
});

describe("administration", () => {
  it("lists accounts and invites a new one", async () => {
    api
      .on("GET /api/v1/users", json({ items: [account()], next_cursor: null }))
      .on(
        "POST /api/v1/users",
        json(account({ id: "u-2", email: "new.person@dev.example", status: "invited" }), 201),
      )
      .on(
        "GET /api/v1/users/u-2",
        json(account({ id: "u-2", email: "new.person@dev.example", status: "invited" })),
      )
      .on("GET /api/v1/users/u-2/roles", json({ items: [] }));
    const user = userEvent.setup();
    const { router } = renderSignedIn(api, HR_ADMIN_ME, "/admin/users");
    expect(await screen.findByRole("link", { name: "test.account@dev.example" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Invite user" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Work email"), "new.person@dev.example");
    await user.click(within(dialog).getByRole("button", { name: "Send invite" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/admin/users/u-2");
    });
    expect(api.called("POST /api/v1/users")[0]?.body).toEqual({ email: "new.person@dev.example" });
  });

  it("disables an account only after confirmation and step-up", async () => {
    api
      .on(`GET /api/v1/users/${USER_ID}`, json(account()))
      .on(`GET /api/v1/users/${USER_ID}/roles`, json({ items: [] }))
      .on(`POST /api/v1/users/${USER_ID}/disable`, problem("step-up-required", 403, { max_age_seconds: 600 }))
      .on(`POST /api/v1/users/${USER_ID}/disable`, json(account({ status: "disabled" })))
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, `/admin/users/${USER_ID}`);
    await user.click(await screen.findByRole("button", { name: "Disable account" }));
    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent("signed out on every device");
    await user.click(within(confirm).getByRole("button", { name: "Disable account" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Account disabled")).toBeInTheDocument();
    expect(api.called(`POST /api/v1/users/${USER_ID}/disable`)).toHaveLength(2);
  });

  it("offers no account actions on one's own account", async () => {
    api
      .on(`GET /api/v1/users/${SUPER_ADMIN_ME.user_id}`, json(account({ id: SUPER_ADMIN_ME.user_id })))
      .on(`GET /api/v1/users/${SUPER_ADMIN_ME.user_id}/roles`, json({ items: [] }));
    renderSignedIn(api, SUPER_ADMIN_ME, `/admin/users/${SUPER_ADMIN_ME.user_id}`);
    expect(await screen.findByText(/This is your own account/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assign role" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable account" })).not.toBeInTheDocument();
  });

  it("shows the audit log with redacted changes and an elevated session flagged", async () => {
    api.on(
      "GET /api/v1/audit-log",
      json({
        items: [
          {
            id: "a-1",
            recorded_at: "2026-10-05T03:30:00Z",
            occurred_at: "2026-10-05T03:30:00Z",
            actor_type: "user",
            actor_user_id: USER_ID,
            session_id: null,
            grant_request_id: "g-1",
            request_id: null,
            ip: null,
            user_agent: null,
            action: "employee.personal.updated",
            permission_used: "employee.personal.update.all",
            target_type: "employee",
            target_id: "e-1",
            subject_employee_id: "e-1",
            outcome: "success",
            changes: { personal_phone: { redacted: true } },
            reason: null,
          },
        ],
        next_cursor: null,
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, SUPER_ADMIN_ME, "/admin/audit-log");
    const table = await screen.findByRole("table", { name: "Audit log" });
    expect(within(table).getByText("Elevated session")).toBeInTheDocument();
    await user.click(within(table).getByText("1 fields"));
    expect(within(table).getByText(/changed \(value not recorded\)/)).toBeInTheDocument();
  });
});
