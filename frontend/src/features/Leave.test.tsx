import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { visibleNavItems } from "../app/navigation";
import { FakeApi, json, problem } from "../test/fakeApi";
import {
  EMPLOYEE_ME,
  HR_ADMIN_ME,
  LEAVE_TYPE,
  MANAGER_ME,
  SUPER_ADMIN_ME,
  leaveBalance,
  leaveRequest,
} from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function conflict(code: string, detail: string) {
  return json(
    { type: "/problems/conflict", title: "Conflict", status: 409, detail, request_id: null, code },
    409,
  );
}

describe("my leave", () => {
  it("shows balances from the API and the requests, with no accessibility violations", async () => {
    api
      .on("GET /api/v1/me/leave-requests", json({ items: [leaveRequest()], next_cursor: null }))
      .on("GET /api/v1/me/leave/balances", json({ items: [leaveBalance()] }))
      .on("GET /api/v1/me/leave/ledger", json({ items: [] }));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/leave");
    const table = await screen.findByRole("table", { name: "My leave requests" });
    expect(within(table).getByText("Waiting for approval")).toBeInTheDocument();
    expect(within(table).getByText("2 days")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request leave" })).toHaveAttribute("href", "/leave/request");
    await user.click(screen.getByRole("tab", { name: "Balances and ledger" }));
    const balances = await screen.findByRole("list", { name: "Leave balances" });
    expect(within(balances).getByText("4 days")).toBeInTheDocument();
    expect(within(balances).getByText(/1 day waiting for approval/)).toBeInTheDocument();
    expect(await axe(document.body)).toHaveNoViolations();
  });

  it("says what to do when no leave type covers the employee", async () => {
    api.on("GET /api/v1/me/leave/balances", json({ items: [] }));
    renderSignedIn(api, EMPLOYEE_ME, "/leave/request");
    expect(await screen.findByText("No leave type is available to you")).toBeInTheDocument();
  });
});

describe("requesting leave", () => {
  it("previews the counted days and sends one request with a stable key", async () => {
    api
      .on("GET /api/v1/me/leave/balances", json({ items: [leaveBalance()] }))
      .on(
        "POST /api/v1/me/leave-requests/preview",
        json({
          days: [
            { date: "2026-11-02", portion: "1.00", counted: true, not_counted_reason: null },
            { date: "2026-11-03", portion: "1.00", counted: false, not_counted_reason: "holiday" },
          ],
          days_requested: "1.00",
          available_before: "4.00",
          available_after: "3.00",
          within_balance: true,
          requires_approval: true,
          weekly_offs_configured: false,
        }),
      )
      .on(
        "POST /api/v1/me/leave-requests",
        conflict("leave.overlap", "You already have leave requested or approved on some of these days."),
      )
      .on("POST /api/v1/me/leave-requests", json(leaveRequest({ id: "r-new" }), 201))
      .on("GET /api/v1/leave-requests/r-new", json(leaveRequest({ id: "r-new" })));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/leave/request");
    await user.click(await screen.findByRole("button", { name: /Send request/ }));
    expect(await screen.findByText("Choose a leave type.")).toBeInTheDocument();
    expect(api.called("POST /api/v1/me/leave-requests")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: /Leave type/ }));
    await user.click(await screen.findByRole("option", { name: /Test leave/ }));
    await user.type(screen.getByLabelText("First day"), "2026-11-02");
    await user.clear(screen.getByLabelText("Last day"));
    await user.type(screen.getByLabelText("Last day"), "2026-11-03");
    const days = await screen.findByRole("list", { name: "Days in this request" });
    expect(within(days).getByText("Holiday, not counted")).toBeInTheDocument();
    expect(screen.getByText(/Weekly offs are not set up/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText(/already have leave requested/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByRole("heading", { name: LEAVE_TYPE.name })).toBeInTheDocument();
    const [first, second] = api.called("POST /api/v1/me/leave-requests");
    expect(first?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(second?.headers.get("Idempotency-Key")).toBe(first?.headers.get("Idempotency-Key"));
    expect(second?.body).toMatchObject({
      leave_type_id: LEAVE_TYPE.id,
      start_date: "2026-11-02",
      end_date: "2026-11-03",
      reason: null,
    });
  });
});

describe("deciding leave", () => {
  it("lets the approver approve with a request key and requires a note to reject", async () => {
    const pending = leaveRequest({ can_decide: true, can_cancel: false });
    api
      .on(`GET /api/v1/leave-requests/${pending.id}`, json(pending))
      .on(`POST /api/v1/leave-requests/${pending.id}/reject`, json({ ...pending, status: "rejected" }));
    const user = userEvent.setup();
    renderSignedIn(api, MANAGER_ME, `/leave/requests/${pending.id}`);
    expect(await screen.findByText("Test family event")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(await within(dialog).findByText(/Say why/)).toBeInTheDocument();
    expect(api.called(`POST /api/v1/leave-requests/${pending.id}/reject`)).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Note to the employee"), "Test: team offsite");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => {
      expect(api.called(`POST /api/v1/leave-requests/${pending.id}/reject`)).toHaveLength(1);
    });
    const [call] = api.called(`POST /api/v1/leave-requests/${pending.id}/reject`);
    expect(call?.body).toEqual({ note: "Test: team offsite" });
    expect(call?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("explains a decision someone else already made", async () => {
    const pending = leaveRequest({ can_decide: true, can_cancel: false });
    api
      .on(`GET /api/v1/leave-requests/${pending.id}`, json(pending))
      .on(
        `POST /api/v1/leave-requests/${pending.id}/approve`,
        conflict("leave.request.already_decided", "This request has already been decided or withdrawn."),
      );
    const user = userEvent.setup();
    renderSignedIn(api, MANAGER_ME, `/leave/requests/${pending.id}`);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await within(dialog).findByText(/already been decided/)).toBeInTheDocument();
  });

  it("hides the reason from someone who only reads leave", async () => {
    const seen = leaveRequest({ reason: null, reason_visible: false, can_cancel: false });
    api.on(`GET /api/v1/leave-requests/${seen.id}`, json(seen));
    renderSignedIn(api, HR_ADMIN_ME, `/leave/requests/${seen.id}`);
    expect(await screen.findByText(/Visible only to the employee/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("lists what waits for the approver, with the employee", async () => {
    api.on("GET /api/v1/leave-requests", json({ items: [leaveRequest()], next_cursor: null }));
    renderSignedIn(api, MANAGER_ME, "/leave/approvals");
    const table = await screen.findByRole("table", { name: "Waiting for your decision" });
    expect(within(table).getByText("Test Person")).toBeInTheDocument();
    expect(api.called("GET /api/v1/leave-requests")[0]?.query.get("awaiting_my_decision")).toBe("true");
  });
});

describe("leave calendar and setup", () => {
  it("shows who is away without reasons and moves between months", async () => {
    api.on(
      "GET /api/v1/leave/calendar",
      json({
        items: [
          {
            request_id: "r-1",
            employee: { id: "e-1", name: "Test Away" },
            leave_type: LEAVE_TYPE,
            start_date: "2026-11-02",
            end_date: "2026-11-03",
            start_half: null,
            end_half: null,
            status: "approved",
          },
        ],
        truncated: false,
      }),
    );
    api.on("GET /api/v1/leave-types", json({ items: [] }));
    const user = userEvent.setup();
    renderSignedIn(api, MANAGER_ME, "/leave/calendar?month=2026-11");
    expect(await screen.findByText("Test Away")).toBeInTheDocument();
    expect(api.called("GET /api/v1/leave/calendar")[0]?.query.get("from")).toBe("2026-11-01");
    await user.click(screen.getByRole("button", { name: "Next month" }));
    await waitFor(() => {
      expect(api.called("GET /api/v1/leave/calendar").at(-1)?.query.get("from")).toBe("2026-12-01");
    });
  });

  it("says what archiving a leave type does and shows why it was refused", async () => {
    const leaveType = {
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
      version: 3,
      created_at: "2026-10-11T00:00:00Z",
      updated_at: "2026-10-11T00:00:00Z",
    };
    api
      .on("GET /api/v1/leave-types", json({ items: [leaveType] }))
      .on("GET /api/v1/leave-policies", json({ items: [] }))
      .on(
        "PATCH /api/v1/leave-types/t-1",
        problem("conflict", 409, {
          code: "leave.type_in_use",
          detail: "1 request of this leave type waits for a decision. Decide them first, then archive it.",
        }),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/setup/leave-types");
    await user.click(await screen.findByRole("button", { name: "Edit Test leave" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Save changes" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Status/ }));
    await user.click(await screen.findByRole("option", { name: "Archived (no new requests)" }));
    expect(within(dialog).getByText(/Archiving stops new requests of this type/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive leave type" }));
    expect(await within(dialog).findByText(/1 request of this leave type waits/)).toBeInTheDocument();
    const [call] = api.called("PATCH /api/v1/leave-types/t-1");
    expect(call?.headers.get("If-Match")).toBe('"3"');
    expect(call?.body).toMatchObject({ status: "archived" });
  });

  it("asks HR for every rule of a new leave type instead of assuming one", async () => {
    api
      .on("GET /api/v1/leave-types", json({ items: [] }))
      .on("GET /api/v1/leave-policies", json({ items: [] }))
      .on(
        "POST /api/v1/leave-types",
        json(
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
            created_at: "2026-10-11T00:00:00Z",
            updated_at: "2026-10-11T00:00:00Z",
          },
          201,
        ),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/setup/leave-types");
    expect(await screen.findByText("No leave types yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add leave type" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "TL");
    await user.type(within(dialog).getByLabelText("Name"), "Test leave");
    await user.click(within(dialog).getByRole("button", { name: "Add leave type" }));
    expect(await within(dialog).findAllByText("Choose yes or no.")).toHaveLength(5);
    expect(api.called("POST /api/v1/leave-types")).toHaveLength(0);
    for (const [label, answer] of [
      ["Paid leave", "Yes"],
      ["Needs approval", "Yes"],
      ["Half days allowed", "No"],
      ["Holidays count as leave", "No"],
      ["Weekly offs count as leave", "No"],
    ] as const) {
      await user.click(within(dialog).getByRole("button", { name: new RegExp(label) }));
      await user.click(await screen.findByRole("option", { name: answer }));
    }
    await user.click(within(dialog).getByRole("button", { name: "Add leave type" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/leave-types")).toHaveLength(1);
    });
    expect(api.called("POST /api/v1/leave-types")[0]?.body).toEqual({
      code: "TL",
      name: "Test leave",
      description: null,
      is_paid: true,
      requires_approval: true,
      allows_half_day: false,
      counts_holidays: false,
      counts_weekly_offs: false,
    });
  });
});

describe("leave navigation", () => {
  it("shows each person only the leave pages their permissions reach", () => {
    const paths = (me: typeof EMPLOYEE_ME) => visibleNavItems(me).map((item) => item.to);
    expect(paths(EMPLOYEE_ME)).toContain("/leave");
    expect(paths(EMPLOYEE_ME)).not.toContain("/leave/approvals");
    expect(paths(MANAGER_ME)).toEqual(expect.arrayContaining(["/leave/approvals", "/leave/calendar"]));
    expect(paths(HR_ADMIN_ME)).toEqual(
      expect.arrayContaining(["/leave/overview", "/leave/balances", "/setup/leave-types"]),
    );
    expect(
      paths(SUPER_ADMIN_ME).filter((path) => path.startsWith("/leave") || path.includes("leave")),
    ).toEqual([]);
  });
});
