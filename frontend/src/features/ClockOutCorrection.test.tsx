import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import type { components } from "../lib/api-schema";
import { FakeApi, json, problem } from "../test/fakeApi";
import { HR_ADMIN_ME, MANAGER_ME, attendanceDay } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const HR_ME = {
  ...HR_ADMIN_ME,
  permissions: [...HR_ADMIN_ME.permissions, "attendance.correction.create.all"],
};

const PERSON = {
  id: "e-1",
  name: "Test Forgetful",
  employee_code: "TEST-1",
  department: "Test department",
  designation: "Test designation",
  location: "Test location",
};

function member(
  overrides: Partial<components["schemas"]["TeamMemberToday"]> = {},
): components["schemas"]["TeamMemberToday"] {
  return {
    employee: PERSON,
    state: "clocked_in",
    work_date: "2026-10-05",
    first_in: "2026-10-05T03:35:00Z",
    last_event_at: "2026-10-05T03:35:00Z",
    session_seconds: 0,
    break_seconds: 0,
    time_zone: "Asia/Kolkata",
    open_since: "2026-10-05",
    ...overrides,
  };
}

function today(items: components["schemas"]["TeamMemberToday"][]) {
  return json({
    summary: { in_scope: 1, clocked_in_today: 0, working_now: 1, on_break: 0 },
    items,
    next_cursor: null,
  });
}

const RECORDED = json(
  {
    correction: { id: "c-1" },
  },
  201,
);

describe("correcting a forgotten clock-out", () => {
  it("flags the open session and closes it at the verified time with a reason", async () => {
    api
      .on("GET /api/v1/attendance/today", today([member()]))
      .on("POST /api/v1/employees/e-1/attendance/corrections", RECORDED);
    const user = userEvent.setup();
    renderSignedIn(api, HR_ME, "/team/attendance");
    const table = await screen.findByRole("table", { name: "Team attendance today" });
    expect(within(table).getByText(/No clock-out since/)).toBeInTheDocument();
    // The open session is not counted as time worked.
    expect(within(table).getByText("Unknown")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: /Correct clock-out/ }));
    const dialog = await screen.findByRole("dialog", { name: /Correct the clock-out for Test Forgetful/ });
    expect(dialog).toHaveTextContent("Asia/Kolkata");
    expect(dialog).toHaveTextContent("09:05");
    expect(await axe(dialog)).toHaveNoViolations();

    // No time is assumed, and a reason is required.
    await user.click(within(dialog).getByRole("button", { name: "Correct clock-out" }));
    expect(within(dialog).getByText("Enter the time they left.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Left at"), "17:40");
    await user.click(within(dialog).getByRole("button", { name: "Correct clock-out" }));
    expect(within(dialog).getByText("Say how the departure time was verified.")).toBeInTheDocument();
    expect(api.called("POST /api/v1/employees/e-1/attendance/corrections")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText("Reason"), "Gate log shows 17:40");
    await user.click(within(dialog).getByRole("button", { name: "Correct clock-out" }));
    expect(await screen.findByText("Clock-out corrected")).toBeInTheDocument();
    const [call] = api.called("POST /api/v1/employees/e-1/attendance/corrections");
    expect(call?.body).toEqual({
      work_date: "2026-10-05",
      reason: "Gate log shows 17:40",
      items: [{ action: "add", event_type: "CLOCK_OUT", proposed_at: "2026-10-05T12:10:00.000Z" }],
    });
    expect(call?.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("adds the break's end when the session was left open on a break", async () => {
    api
      .on("GET /api/v1/attendance/today", today([member({ state: "on_break" })]))
      .on("POST /api/v1/employees/e-1/attendance/corrections", RECORDED);
    const user = userEvent.setup();
    renderSignedIn(api, HR_ME, "/team/attendance");
    await user.click(await screen.findByRole("button", { name: /Correct clock-out/ }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox", { name: "Their break ended before they left" }));
    await user.type(within(dialog).getByLabelText("Break ended at"), "13:45");
    await user.type(within(dialog).getByLabelText("Left at"), "17:45");
    await user.type(within(dialog).getByLabelText("Reason"), "Badge log");
    await user.click(within(dialog).getByRole("button", { name: "Correct clock-out" }));
    expect(await screen.findByText("Clock-out corrected")).toBeInTheDocument();
    const body = api.called("POST /api/v1/employees/e-1/attendance/corrections")[0]?.body as {
      items: { event_type: string }[];
    };
    expect(body.items.map((item) => item.event_type)).toEqual(["BREAK_END", "CLOCK_OUT"]);
  });

  it("shows what the API refused and keeps the dialog open", async () => {
    api.on("GET /api/v1/attendance/today", today([member()])).on(
      "POST /api/v1/employees/e-1/attendance/corrections",
      problem("validation-error", 422, {
        errors: [{ field: "items", code: "correction.sequence", message: "The day would not make sense." }],
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ME, "/team/attendance");
    await user.click(await screen.findByRole("button", { name: /Correct clock-out/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Left at"), "08:00");
    await user.type(within(dialog).getByLabelText("Reason"), "Gate log");
    await user.click(within(dialog).getByRole("button", { name: "Correct clock-out" }));
    expect(await within(dialog).findByText(/Check the times|would not make sense/)).toBeInTheDocument();
  });

  it("offers the action in the daily register for an incomplete day", async () => {
    api.on(
      "GET /api/v1/attendance/register",
      json({
        date: "2026-10-05",
        in_scope: 1,
        counts: { incomplete: 1 },
        items: [
          {
            employee: PERSON,
            time_zone: "Asia/Kolkata",
            day: attendanceDay({
              open_session: true,
              status: "incomplete",
              judged: true,
              first_in: "2026-10-05T03:35:00Z",
            }),
          },
        ],
        next_cursor: null,
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ME, "/team/attendance");
    await user.click(await screen.findByRole("tab", { name: "Daily register" }));
    const table = await screen.findByRole("table", { name: "Attendance register" });
    await user.click(within(table).getByRole("button", { name: /Correct clock-out/ }));
    expect(await screen.findByRole("dialog", { name: /Correct the clock-out/ })).toBeInTheDocument();
  });

  it("is not offered without attendance.correction.create.all", async () => {
    api.on("GET /api/v1/attendance/today", today([member()]));
    renderSignedIn(api, MANAGER_ME, "/team/attendance");
    const table = await screen.findByRole("table", { name: "Team attendance today" });
    expect(within(table).getByText(/No clock-out since/)).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: /Correct clock-out/ })).not.toBeInTheDocument();
  });
});
