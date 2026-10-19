/**
 * The data views in a real browser: attendance days open to their events in time order with
 * corrections marked; the month view navigates by buttons and arrow keys and opens a day; the
 * audit log opens a record's full context; departments show as an expandable hierarchy whose
 * children render only when opened. Each passes axe in both themes and works at 200 % zoom,
 * and details appear without motion when reduced motion is asked for. The clock is fixed and
 * every record is obviously fake.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

import roles from "../src/test/role-permissions.json" with { type: "json" };
import { HR_ADMIN, fakeApi } from "./support";

const NOW = new Date("2031-03-12T05:30:00Z"); // 11:00 in Asia/Kolkata
const ZONE = "Asia/Kolkata";

const EMPLOYEE = {
  ...HR_ADMIN,
  email: "test.employee@dev.example",
  employee_id: "01900000-0000-7000-8000-0000000000e1",
  roles: ["employee"],
  permissions: [...new Set([...roles.account_baseline, ...roles.employee])],
};

const AUDITOR = {
  ...HR_ADMIN,
  email: "test.auditor@dev.example",
  roles: ["auditor"],
  permissions: [...new Set([...roles.account_baseline, ...roles.auditor])],
};

function event(id: string, type: string, at: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    event_type: type,
    occurred_at: at,
    source: "web",
    voided: false,
    correction_id: null,
    ...extra,
  };
}

function day(date: string, extra: Record<string, unknown> = {}) {
  return {
    work_date: date,
    first_in: null,
    last_out: null,
    session_seconds: 0,
    break_seconds: 0,
    open_session: false,
    on_break: false,
    missing_break_end: false,
    status: null,
    judged: true,
    shift_name: "TEST-DAY",
    shift_start_at: `${date}T03:30:00Z`,
    shift_end_at: `${date}T12:30:00Z`,
    effective_minutes: 0,
    late_minutes: 0,
    early_leave_minutes: 0,
    overtime_minutes: 0,
    anomalies: [],
    holiday: false,
    leave_portion: "0",
    events: [],
    ...extra,
  };
}

const MARCH = [
  day("2031-03-07", { status: "absent" }),
  day("2031-03-08", { status: "weekly_off" }),
  day("2031-03-10", {
    status: "present",
    first_in: "2031-03-10T03:30:00Z",
    last_out: "2031-03-10T12:30:00Z",
    session_seconds: 32400,
    break_seconds: 1800,
    effective_minutes: 510,
    // Deliberately out of order: the timeline sorts by time.
    events: [
      event("e-4", "CLOCK_OUT", "2031-03-10T12:30:00Z"),
      event("e-1", "CLOCK_IN", "2031-03-10T03:30:00Z"),
      event("e-3", "BREAK_END", "2031-03-10T08:00:00Z"),
      event("e-2", "BREAK_START", "2031-03-10T07:30:00Z"),
    ],
  }),
  day("2031-03-11", {
    status: "present",
    first_in: "2031-03-11T03:40:00Z",
    last_out: "2031-03-11T12:45:00Z",
    session_seconds: 32700,
    effective_minutes: 545,
    late_minutes: 10,
    events: [
      event("f-1", "CLOCK_IN", "2031-03-11T03:40:00Z"),
      event("f-2", "CLOCK_OUT", "2031-03-11T10:00:00Z", { voided: true }),
      event("f-3", "CLOCK_OUT", "2031-03-11T12:45:00Z", { source: "correction", correction_id: "c-9" }),
    ],
  }),
];

const FEBRUARY = [day("2031-02-27", { status: "on_leave", leave_portion: "1" })];

const AUDIT = {
  items: [
    {
      id: "a-1",
      recorded_at: "2031-03-12T05:00:01Z",
      occurred_at: "2031-03-12T05:00:00Z",
      actor_type: "user",
      actor_user_id: "01900000-0000-7000-8000-0000000000aa",
      session_id: "01900000-0000-7000-8000-0000000000bb",
      grant_request_id: null,
      request_id: "req-test-1",
      ip: "192.0.2.10",
      user_agent: "Test browser",
      action: "holiday.updated",
      permission_used: "holiday.manage",
      target_type: "holiday_calendar",
      target_id: "01900000-0000-7000-8000-0000000000cc",
      subject_employee_id: null,
      outcome: "success",
      changes: { name: { old: "Test holiday", new: "Test renamed" } },
      reason: null,
    },
    {
      id: "a-2",
      recorded_at: "2031-03-12T04:00:01Z",
      occurred_at: "2031-03-12T04:00:00Z",
      actor_type: "user",
      actor_user_id: "01900000-0000-7000-8000-0000000000ab",
      session_id: null,
      grant_request_id: null,
      request_id: "req-test-2",
      ip: "192.0.2.11",
      user_agent: "Test browser",
      action: "role.assign",
      permission_used: null,
      target_type: null,
      target_id: null,
      subject_employee_id: null,
      outcome: "denied",
      changes: {},
      reason: null,
    },
  ],
  next_cursor: null,
};

function department(id: string, name: string, parent: string | null) {
  return {
    id,
    code: id.toUpperCase(),
    name,
    description: null,
    parent_id: parent,
    head_employee_id: null,
    head_name: null,
    status: "active",
    version: 1,
    created_at: "2031-01-01T00:00:00Z",
    updated_at: "2031-01-01T00:00:00Z",
  };
}

const DEPARTMENTS = {
  items: [
    department("d-ops", "Test operations", null),
    department("d-fin", "Test finance", null),
    department("d-pay", "Test payroll", "d-fin"),
    department("d-ap", "Test payables", "d-fin"),
    department("d-pin", "Test payroll south", "d-pay"),
  ],
  next_cursor: null,
};

async function serve(page: Page, me: object, theme: "light" | "dark" = "light") {
  await page.clock.setFixedTime(NOW);
  await page.addInitScript((value) => {
    window.localStorage.setItem("sv-theme", value);
  }, theme);
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const reply = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    switch (url.pathname) {
      case "/api/v1/me":
        return reply(me);
      case "/api/v1/me/attendance/days": {
        const from = url.searchParams.get("from") ?? "";
        const to = url.searchParams.get("to") ?? "";
        const items = [...MARCH, ...FEBRUARY].filter((d) => d.work_date >= from && d.work_date <= to);
        return reply({ time_zone: ZONE, items });
      }
      case "/api/v1/me/attendance/today":
        return reply({
          state: "clocked_out",
          work_date: "2031-03-12",
          time_zone: ZONE,
          server_time: NOW.toISOString(),
          allowed_events: ["CLOCK_IN"],
          open_since: null,
          day: day("2031-03-12"),
        });
      case "/api/v1/audit-log":
        return reply(AUDIT);
      case "/api/v1/departments":
        return reply(DEPARTMENTS);
      default:
        return fakeApi(route);
    }
  });
}

async function axe(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

test("an attendance day opens to its events in time order, with corrections marked", async ({ page }) => {
  await serve(page, EMPLOYEE);
  await page.goto("/attendance");
  const table = page.getByRole("table", { name: "Your attendance days" });
  await expect(table).toBeVisible();
  await table.getByRole("button", { name: /^Show details for .*10 Mar/ }).click();
  await expect(table.getByRole("button", { name: /^Hide details for .*10 Mar/ })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  const events = page.getByRole("list", { name: /Events on .*10 Mar/ });
  await expect(events.getByRole("listitem")).toHaveText([
    /09:00 am.*Clocked in/,
    /01:00 pm.*Break started/,
    /01:30 pm.*Break ended/,
    /06:00 pm.*Clocked out/,
  ]);

  await table.getByRole("button", { name: /^Show details for .*11 Mar/ }).click();
  const corrected = page.getByRole("list", { name: /Events on .*11 Mar/ });
  await expect(corrected.getByRole("listitem").nth(1)).toContainText("Removed by a correction");
  await expect(corrected.getByRole("listitem").nth(2)).toContainText("Added by a correction");
  expect(
    await corrected
      .getByRole("listitem")
      .nth(1)
      .getByText("03:30 pm")
      .evaluate((node) => getComputedStyle(node).textDecorationLine),
  ).toBe("line-through");
  expect(await axe(page)).toEqual([]);
});

test("the month view shows each day's status, moves by buttons and keys, and opens a day", async ({
  page,
}) => {
  await serve(page, EMPLOYEE);
  await page.goto("/attendance");
  await page.getByRole("radio", { name: "Month" }).click();
  const calendar = page.getByRole("application", { name: /Attendance by day/ });
  await expect(calendar.getByRole("heading", { name: "March 2031" })).toBeVisible();
  const grid = calendar.getByRole("grid");
  await expect(grid.locator('[data-status="present"]')).toHaveCount(2);
  await expect(grid.locator('[data-status="absent"]')).toHaveCount(1);
  await expect(page.getByRole("list", { name: "Key" })).toContainText("Weekly off");
  // Days after today cannot be chosen, and there is no later month to move to.
  await expect(calendar.locator("button[slot=next]")).toBeDisabled();

  await grid.getByRole("button", { name: /11/ }).first().click();
  await expect(page.getByRole("heading", { name: /11 Mar, 2031/ })).toBeVisible();
  await expect(page.getByRole("list", { name: /Events on .*11 Mar/ }).getByRole("listitem")).toHaveCount(3);

  // Keyboard: from the 11th, the left arrow and Enter choose the 10th.
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: /10 Mar, 2031/ })).toBeVisible();

  const february = page.waitForRequest((request) => request.url().includes("from=2031-02-01"));
  await calendar.locator("button[slot=previous]").click();
  await february;
  await expect(calendar.getByRole("heading", { name: "February 2031" })).toBeVisible();
  await expect(grid.locator('[data-status="on_leave"]')).toHaveCount(1);
  expect(await axe(page)).toEqual([]);
});

test("an audit record opens to its changes and request context", async ({ page }) => {
  await serve(page, AUDITOR);
  await page.goto("/admin/audit-log");
  const table = page.getByRole("table", { name: "Audit log" });
  await expect(table.getByText("Refused")).toBeVisible();
  await table.getByRole("button", { name: /^Show details for holiday.updated/ }).click();
  const toggle = table.getByRole("button", { name: /^Hide details for holiday.updated/ });
  const panelId = (await toggle.getAttribute("aria-controls")) ?? "";
  const panel = page.locator(`[id="${panelId}"]`);
  await expect(panel).toContainText('"Test holiday" to "Test renamed"');
  await expect(panel).toContainText("req-test-1");
  await expect(panel).toContainText("192.0.2.10");
  expect(await axe(page)).toEqual([]);
});

/** The focused element's outline, when the browser treats the focus as keyboard focus. */
async function focusOutline(page: Page) {
  return page.evaluate(() => {
    const focused = document.activeElement;
    if (!focused?.matches(":focus-visible")) return null;
    const style = getComputedStyle(focused);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
}

test("audit details and the department hierarchy work from the keyboard with a visible focus outline", async ({
  page,
}) => {
  await serve(page, AUDITOR);
  await page.goto("/admin/audit-log");
  const table = page.getByRole("table", { name: "Audit log" });
  const show = table.getByRole("button", { name: /^Show details for holiday.updated/ });
  await show.focus();
  expect(await focusOutline(page)).toEqual({ width: "2px", style: "solid" });
  await page.keyboard.press("Enter");
  const hide = table.getByRole("button", { name: /^Hide details for holiday.updated/ });
  await expect(hide).toBeFocused();
  await expect(hide).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(`[id="${(await hide.getAttribute("aria-controls")) ?? ""}"]`)).toContainText(
    "req-test-1",
  );
  await page.keyboard.press("Space");
  await expect(table.getByRole("button", { name: /^Show details for holiday.updated/ })).toHaveAttribute(
    "aria-expanded",
    "false",
  );

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await serve(page, HR_ADMIN);
  await page.goto("/organization?tab=departments");
  // Keyboard throughout: a pointer click would make the browser hide the focus outline.
  await page.getByRole("radio", { name: "Hierarchy" }).focus();
  await page.keyboard.press("Space");
  const tree = page.getByRole("table", { name: "Department hierarchy" });
  await tree.getByRole("button", { name: /^Expand Test finance/ }).focus();
  expect(await focusOutline(page)).toEqual({ width: "2px", style: "solid" });
  await page.keyboard.press("Enter");
  // Tab moves from the expand control to the department's name; Enter selects it.
  await page.keyboard.press("Tab");
  await expect(tree.getByRole("button", { name: "Test finance", exact: true })).toBeFocused();
  expect(await focusOutline(page)).toEqual({ width: "2px", style: "solid" });
  await page.keyboard.press("Enter");
  await expect(tree.getByRole("button", { name: "Test finance", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByRole("region", { name: "Selected department" })).toContainText("Test payroll");
});

test("departments show as a hierarchy whose children render only when opened", async ({ page }) => {
  await serve(page, HR_ADMIN);
  await page.goto("/organization?tab=departments");
  await page.getByRole("radio", { name: "Hierarchy" }).click();
  const tree = page.getByRole("table", { name: "Department hierarchy" });
  await expect(tree.getByRole("button", { name: "Test finance", exact: true })).toBeVisible();
  // Children are not in the page until their parent is expanded.
  await expect(tree.getByText("Test payroll")).toHaveCount(0);
  const expand = tree.getByRole("button", { name: "Expand Test finance, 2 sub-departments" });
  await expand.click();
  await expect(
    tree.getByRole("button", { name: "Collapse Test finance, 2 sub-departments" }),
  ).toHaveAttribute("aria-expanded", "true");
  await expect(tree.getByRole("button", { name: "Level 2: Test payroll" })).toBeVisible();
  await expect(tree.getByText("Test payroll south")).toHaveCount(0);

  await tree.getByRole("button", { name: "Level 2: Test payroll" }).click();
  const details = page.getByRole("region", { name: "Selected department" });
  await expect(details).toContainText("Part of");
  await expect(details).toContainText("Test finance");
  await expect(details).toContainText("Test payroll south");
  await expect(details.getByRole("link", { name: "People in Test payroll" })).toHaveAttribute(
    "href",
    "/employees?department=d-pay",
  );

  await page.getByRole("button", { name: "Expand all" }).click();
  await expect(tree.getByRole("button", { name: "Level 3: Test payroll south" })).toBeVisible();
  // Keyboard: the expand control and the name are ordinary buttons in reading order.
  await tree.getByRole("button", { name: /Collapse Test finance/ }).focus();
  await page.keyboard.press("Enter");
  await expect(tree.getByText("Test payroll")).toHaveCount(0);
  expect(await axe(page)).toEqual([]);
});

test("the data views pass axe in the dark theme", async ({ page }) => {
  await serve(page, EMPLOYEE, "dark");
  await page.goto("/attendance");
  await page.getByRole("radio", { name: "Month" }).click();
  await page.getByRole("grid").getByRole("button", { name: /10/ }).first().click();
  await expect(page.getByRole("heading", { name: /10 Mar, 2031/ })).toBeVisible();
  expect(await axe(page)).toEqual([]);

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await serve(page, HR_ADMIN, "dark");
  await page.goto("/organization?tab=departments");
  await page.getByRole("radio", { name: "Hierarchy" }).click();
  await page.getByRole("button", { name: "Expand all" }).click();
  await page
    .getByRole("table", { name: "Department hierarchy" })
    .getByRole("button", { name: "Test finance", exact: true })
    .click();
  expect(await axe(page)).toEqual([]);
});

test("details appear without motion when reduced motion is asked for", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await serve(page, EMPLOYEE);
  await page.goto("/attendance");
  await page
    .getByRole("table", { name: "Your attendance days" })
    .getByRole("button", { name: /^Show details for .*10 Mar/ })
    .click();
  const reveal = page.locator(".animate-reveal").first();
  await expect(reveal).toBeVisible();
  expect(await reveal.evaluate((node) => getComputedStyle(node).animationDuration)).toBe("0s");
});

test("details animate briefly otherwise", async ({ page }) => {
  await serve(page, EMPLOYEE);
  await page.goto("/attendance");
  await page
    .getByRole("table", { name: "Your attendance days" })
    .getByRole("button", { name: /^Show details for .*10 Mar/ })
    .click();
  const reveal = page.locator(".animate-reveal").first();
  expect(await reveal.evaluate((node) => getComputedStyle(node).animationDuration)).toBe("0.2s");
});

for (const [name, me, path, open] of [
  ["attendance month", EMPLOYEE, "/attendance", "Month"],
  ["department hierarchy", HR_ADMIN, "/organization?tab=departments", "Hierarchy"],
  ["audit log", AUDITOR, "/admin/audit-log", null],
] as const) {
  test(`the ${name} works at 200 % zoom without sideways scrolling`, async ({ page }) => {
    await page.setViewportSize({ width: 640, height: 360 });
    await serve(page, me);
    await page.goto(path);
    if (open) await page.getByRole("radio", { name: open }).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    ).toBeLessThanOrEqual(0);
  });
}
