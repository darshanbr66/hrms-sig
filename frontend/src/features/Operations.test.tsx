import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { visibleNavItems } from "../app/navigation";
import type { Me } from "../app/session";
import { FakeApi, json, problem } from "../test/fakeApi";
import { EMPLOYEE_ME, HR_ADMIN_ME, MANAGER_ME, attendanceDay, today } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const BASELINE = ["notification.read.self", "notification.preference.update.self"];
const EMPLOYEE: Me = {
  ...EMPLOYEE_ME,
  permissions: [
    ...EMPLOYEE_ME.permissions,
    ...BASELINE,
    "attendance.correction.request.self",
    "document.read.self",
    "document.upload.self",
    "document.company.read",
    "shift.read",
  ],
};
const MANAGER: Me = {
  ...MANAGER_ME,
  permissions: [
    ...MANAGER_ME.permissions,
    ...BASELINE,
    "attendance.correction.approve.team",
    "document.read.team",
  ],
};
const HR_ADMIN: Me = {
  ...HR_ADMIN_ME,
  permissions: [
    ...HR_ADMIN_ME.permissions,
    ...BASELINE,
    "shift.read",
    "shift.manage",
    "shift.assign.all",
    "attendance.policy.manage",
    "attendance.correction.approve.all",
    "attendance.correction.create.all",
    "attendance.export",
    "leave.export",
    "employee.export",
    "document.read.all",
    "document.manage.all",
    "document.category.manage",
    "document.company.manage",
  ],
};

const CORRECTION_ID = "01900000-0000-7000-8000-0000000000c1";
const correctionDetail = (overrides: Record<string, unknown> = {}) => ({
  correction: {
    id: CORRECTION_ID,
    employee_id: "01900000-0000-7000-8000-0000000000e1",
    employee_name: "Test Person",
    work_date: "2026-10-02",
    kind: "request",
    status: "pending",
    requested_at: "2026-10-03T05:00:00Z",
    decided_at: null,
    locked_period: false,
  },
  reason: "Test: forgot to clock out",
  decision_note: null,
  requested_by: "person@dev.example",
  decided_by: null,
  items: [
    { action: "add", event_type: "CLOCK_OUT", proposed_at: "2026-10-02T12:30:00Z", target_event_id: null },
  ],
  time_zone: "Asia/Kolkata",
  before: [{ event_type: "CLOCK_IN", occurred_at: "2026-10-02T03:30:00Z" }],
  after: [
    { event_type: "CLOCK_IN", occurred_at: "2026-10-02T03:30:00Z" },
    { event_type: "CLOCK_OUT", occurred_at: "2026-10-02T12:30:00Z" },
  ],
  still_valid: true,
  can_decide: true,
  can_cancel: false,
  version: 1,
  ...overrides,
});

describe("navigation for the new areas", () => {
  it("shows each area only to the people who can use it", () => {
    const paths = (me: Me) => visibleNavItems(me).map((item) => item.to);
    expect(paths(EMPLOYEE)).toEqual(expect.arrayContaining(["/documents"]));
    expect(paths(EMPLOYEE)).not.toContain("/setup/attendance");
    expect(paths(EMPLOYEE)).not.toContain("/attendance/corrections");
    expect(paths(MANAGER)).toContain("/attendance/corrections");
    expect(paths(HR_ADMIN)).toEqual(
      expect.arrayContaining(["/setup/attendance", "/setup/document-categories", "/reports"]),
    );
  });
});

describe("notifications", () => {
  it("shows the unread count, lists notifications and saves a preference", async () => {
    api
      .on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 2 }))
      .on(
        "GET /api/v1/me/notifications",
        json({
          items: [
            {
              id: "01900000-0000-7000-8000-0000000000n1",
              type: "leave.request_updated",
              title: "Your leave was approved",
              body: "Your leave request was approved.",
              link: "/leave/requests/x",
              read: false,
              created_at: "2026-10-05T03:00:00Z",
            },
          ],
          unread_count: 2,
          next_cursor: null,
        }),
      )
      .on(
        "GET /api/v1/me/notification-preferences",
        json({
          items: [
            {
              type: "leave.request_updated",
              label: "Updates to my leave",
              description: "Your leave is approved.",
              channels: { in_app: true, email: true },
            },
          ],
        }),
      )
      .on(
        "PUT /api/v1/me/notification-preferences",
        json({
          items: [
            {
              type: "leave.request_updated",
              label: "Updates to my leave",
              description: "Your leave is approved.",
              channels: { in_app: true, email: false },
            },
          ],
        }),
      );
    const user = userEvent.setup();
    const { container } = renderSignedIn(api, EMPLOYEE, "/notifications");
    expect(await screen.findByRole("link", { name: "Notifications, 2 unread" })).toBeInTheDocument();
    const list = await screen.findByRole("list", { name: "Notifications" });
    expect(within(list).getByText("Your leave was approved")).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole("tab", { name: "What I receive" }));
    await user.click(await screen.findByRole("checkbox", { name: "By email" }));
    await waitFor(() => {
      expect(api.called("PUT /api/v1/me/notification-preferences")).toHaveLength(1);
    });
    expect(api.called("PUT /api/v1/me/notification-preferences")[0]?.body).toEqual({
      changes: [{ type: "leave.request_updated", channel: "email", enabled: false }],
    });
    // The badge polls in the background, so it never keeps an idle session alive.
    expect(api.called("GET /api/v1/me/notifications/unread-count")[0]?.headers.get("X-SV-Background")).toBe(
      "1",
    );
  });
});

describe("notification preferences", () => {
  const PREFERENCE = {
    type: "leave.request_updated",
    label: "Updates to my leave",
    description: "Your leave is approved.",
    channels: { in_app: true, email: false },
  };

  it("shows a change at once and puts it back when the save is refused", async () => {
    api
      .on("GET /api/v1/me/notifications", json({ items: [], unread_count: 0, next_cursor: null }))
      .on("GET /api/v1/me/notification-preferences", json({ items: [PREFERENCE] }))
      .on("PUT /api/v1/me/notification-preferences", problem("service-unavailable", 503));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE, "/notifications");
    await user.click(await screen.findByRole("tab", { name: "What I receive" }));
    const email = await screen.findByRole("checkbox", { name: "By email" });
    expect(email).not.toBeChecked();
    await user.click(email);
    expect(await screen.findByText("Problem")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "By email" })).not.toBeChecked();
    });
    expect(screen.queryByText("Preference saved")).not.toBeInTheDocument();
  });

  it("keeps the saved state, and toggles from the keyboard", async () => {
    const saved = { items: [{ ...PREFERENCE, channels: { in_app: true, email: true } }] };
    api
      .on("GET /api/v1/me/notifications", json({ items: [], unread_count: 0, next_cursor: null }))
      .on("GET /api/v1/me/notification-preferences", json({ items: [PREFERENCE] }))
      .on("GET /api/v1/me/notification-preferences", json(saved))
      .on("PUT /api/v1/me/notification-preferences", json(saved));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE, "/notifications");
    await user.click(await screen.findByRole("tab", { name: "What I receive" }));
    const email = await screen.findByRole("checkbox", { name: "By email" });
    email.focus();
    await user.keyboard(" ");
    expect(screen.getByRole("checkbox", { name: "By email" })).toBeChecked();
    expect(await screen.findByText("Preference saved")).toBeInTheDocument();
    expect(api.called("PUT /api/v1/me/notification-preferences")[0]?.body).toEqual({
      changes: [{ type: "leave.request_updated", channel: "email", enabled: true }],
    });
    // The saved list is read back from the API and still shows the box checked.
    await waitFor(() => {
      expect(api.called("GET /api/v1/me/notification-preferences").length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.getByRole("checkbox", { name: "By email" })).toBeChecked();
  });
});

describe("attendance corrections", () => {
  it("asks for a correction with times on the employee's clock and one request key", async () => {
    api
      .on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 0 }))
      .on("GET /api/v1/me/attendance/today", json(today()))
      .on(
        "GET /api/v1/me/attendance/days",
        json({
          time_zone: "Asia/Kolkata",
          items: [
            attendanceDay({
              work_date: "2026-10-02",
              first_in: "2026-10-02T03:30:00Z",
              open_session: true,
              status: "incomplete",
              judged: true,
              events: [
                {
                  id: "01900000-0000-7000-8000-0000000000v1",
                  event_type: "CLOCK_IN",
                  occurred_at: "2026-10-02T03:30:00Z",
                  source: "web",
                  voided: false,
                  correction_id: null,
                },
              ],
            }),
          ],
        }),
      )
      .on(
        "POST /api/v1/me/attendance/corrections",
        json(correctionDetail({ can_decide: false, can_cancel: true }), 201),
      )
      .on(
        "GET /api/v1/attendance/corrections/" + CORRECTION_ID,
        json(correctionDetail({ can_decide: false, can_cancel: true })),
      );
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE, "/attendance");
    const table = await screen.findByRole("table", { name: "Your attendance days" });
    expect(within(table).getByText("No clock-out")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: /Correct/ }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Event 1/ }));
    await user.click(await screen.findByRole("option", { name: "Clock out" }));
    await user.type(within(dialog).getByLabelText("Time of event 1"), "18:00");
    await user.type(within(dialog).getByLabelText("Reason"), "Test: forgot to clock out");
    await user.click(within(dialog).getByRole("button", { name: "Send for decision" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/me/attendance/corrections")).toHaveLength(1);
    });
    const [call] = api.called("POST /api/v1/me/attendance/corrections");
    expect(call?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(call?.body).toEqual({
      work_date: "2026-10-02",
      reason: "Test: forgot to clock out",
      // 18:00 in Asia/Kolkata.
      items: [{ action: "add", event_type: "CLOCK_OUT", proposed_at: "2026-10-02T12:30:00.000Z" }],
    });
    expect(await screen.findByRole("heading", { name: /Correction for/ })).toBeInTheDocument();
  });

  it("lets the manager see both timelines and approve", async () => {
    api
      .on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 0 }))
      .on("GET /api/v1/attendance/corrections/" + CORRECTION_ID, json(correctionDetail()))
      .on(
        "POST /api/v1/attendance/corrections/" + CORRECTION_ID + "/approve",
        json(
          correctionDetail({
            can_decide: false,
            correction: { ...correctionDetail().correction, status: "approved" },
          }),
        ),
      );
    const user = userEvent.setup();
    const { container } = renderSignedIn(api, MANAGER, `/attendance/corrections/${CORRECTION_ID}`);
    const before = await screen.findByRole("list", { name: "As recorded now" });
    expect(within(before).getAllByRole("listitem")).toHaveLength(1);
    expect(
      within(screen.getByRole("list", { name: "With the correction" })).getAllByRole("listitem"),
    ).toHaveLength(2);
    expect(screen.getByText("Test: forgot to clock out")).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole("button", { name: "Approve correction" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/attendance/corrections/" + CORRECTION_ID + "/approve")).toHaveLength(1);
    });
  });
});

describe("attendance set-up", () => {
  it("shows the set-up gaps and adds a shift with its weekly offs", async () => {
    api
      .on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 0 }))
      .on(
        "GET /api/v1/attendance/setup",
        json({
          date: "2026-10-05",
          active_shifts: 0,
          policies_in_effect: 0,
          organization_policy: false,
          employees_in_scope: 3,
          without_shift: [
            {
              id: "01900000-0000-7000-8000-0000000000e1",
              name: "Test Person",
              employee_code: "TEST-1",
              department: null,
              designation: null,
              location: "Test location",
            },
          ],
          without_policy: [],
        }),
      )
      .on("GET /api/v1/shifts", json({ items: [] }))
      .on(
        "POST /api/v1/shifts",
        json(
          {
            id: "s1",
            code: "DAY",
            name: "Test day",
            description: null,
            status: "active",
            version: 1,
            versions: [],
          },
          201,
        ),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN, "/setup/attendance");
    expect(await screen.findByRole("link", { name: "Test Person" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Shifts" }));
    expect(await screen.findByText("No shifts yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add shift" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "DAY");
    await user.type(within(dialog).getByLabelText("Name"), "Test day");
    await user.type(within(dialog).getByLabelText("Starts at"), "09:00");
    await user.type(within(dialog).getByLabelText("Ends at"), "17:30");
    await user.click(within(dialog).getByRole("checkbox", { name: "Saturday" }));
    await user.type(within(dialog).getByLabelText(/Weeks of the month for Saturday/), "2, 4");
    await user.click(within(dialog).getByRole("checkbox", { name: "Sunday" }));
    await user.click(within(dialog).getByRole("button", { name: "Add shift" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/shifts")).toHaveLength(1);
    });
    const body = api.called("POST /api/v1/shifts")[0]?.body as { version: { weekly_offs: unknown } };
    expect(body.version.weekly_offs).toEqual([
      { weekday: 6, weeks: [2, 4] },
      { weekday: 7, weeks: null },
    ]);
  });
});

describe("documents", () => {
  it("lists documents with their scan state and says when storage is not set up", async () => {
    api.on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 0 })).on(
      "GET /api/v1/me/documents",
      json({
        storage_ready: false,
        sensitive_hidden: 0,
        items: [
          {
            id: "d1",
            owner_employee_id: "01900000-0000-7000-8000-0000000000e1",
            owner_name: null,
            category: {
              id: "c1",
              code: "LETTERS",
              name: "Test letters",
              description: null,
              sensitivity: "internal",
              employee_visible: true,
              employee_can_upload: true,
              requires_expiry: false,
              status: "active",
              version: 1,
            },
            title: "Test offer letter",
            expires_on: null,
            status: "active",
            version: 1,
            created_at: "2026-10-01T05:00:00Z",
            versions: [
              {
                id: "v1",
                version_no: 1,
                original_filename: "offer.pdf",
                content_type: "application/pdf",
                size_bytes: 20480,
                scan_status: "infected",
                uploaded_at: "2026-10-01T05:00:00Z",
              },
            ],
            can_manage: false,
            can_add_version: true,
          },
        ],
      }),
    );
    const { container } = renderSignedIn(api, EMPLOYEE, "/documents");
    expect(await screen.findByText("Test offer letter")).toBeInTheDocument();
    expect(screen.getByText("Blocked: failed the virus check")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
    expect(screen.getByText(/File storage is not set up/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload a document" })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("reports and exports", () => {
  it("asks for an export and lists the exports with their state", async () => {
    api
      .on("GET /api/v1/me/notifications/unread-count", json({ unread_count: 0 }))
      .on("GET /api/v1/departments", json({ items: [], next_cursor: null }))
      .on("GET /api/v1/locations", json({ items: [], next_cursor: null }))
      .on(
        "GET /api/v1/me/exports",
        json({
          storage_ready: true,
          items: [
            {
              id: "x1",
              export_type: "employees",
              status: "done",
              start: null,
              end: null,
              requested_at: "2026-10-05T03:00:00Z",
              completed_at: "2026-10-05T03:01:00Z",
              expires_at: "2026-10-06T03:01:00Z",
              downloaded_at: null,
              row_count: 12,
              file_name: "employees-2026-10-05.csv",
            },
          ],
        }),
      )
      .on(
        "POST /api/v1/exports/attendance-summary",
        json(
          {
            id: "x2",
            export_type: "attendance_summary",
            status: "queued",
            start: "2026-10-01",
            end: "2026-10-05",
            requested_at: "2026-10-05T03:00:00Z",
            completed_at: null,
            expires_at: null,
            downloaded_at: null,
            row_count: null,
            file_name: null,
          },
          202,
        ),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN, "/reports");
    await user.click(await screen.findByRole("tab", { name: "Exports" }));
    const table = await screen.findByRole("table", { name: "My exports" });
    expect(within(table).getByText("Ready")).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: /Download/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ask for export" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/exports/attendance-summary")).toHaveLength(1);
    });
  });
});
