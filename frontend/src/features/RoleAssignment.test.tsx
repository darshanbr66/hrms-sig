import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { FakeApi, json, problem } from "../test/fakeApi";
import { HR_ADMIN_ME, SUPER_ADMIN_ME } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const USER_ID = "01900000-0000-7000-8000-0000000000b1";
const PATH = `/admin/users/${USER_ID}`;

const ROLES = [
  ["employee", "Employee", true],
  ["manager", "Manager", true],
  ["hr", "HR", false],
  ["hr_admin", "HR admin", false],
  ["payroll_admin", "Payroll admin", false],
  ["system_admin", "System admin", false],
  ["super_admin", "Super admin", false],
  ["auditor", "Auditor", false],
] as const;

function account() {
  return {
    id: USER_ID,
    email: "role.target@dev.example",
    status: "active",
    employee_id: null,
    locked: false,
    mfa_reenrolment_required: false,
    last_login_at: null,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function assignment(role_key: string, id = `a-${role_key}`) {
  return {
    id,
    role_key,
    department_id: null,
    location_id: null,
    valid_from: "2026-01-01T00:00:00Z",
    valid_until: null,
    granted_by: "someone",
    grant_reason: "Test assignment",
    grant_request_id: null,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function setUp(held: string[]) {
  api
    .on(`GET /api/v1/users/${USER_ID}`, json(account()))
    .on(`GET /api/v1/users/${USER_ID}/roles`, json({ items: held.map((key) => assignment(key)) }))
    .on(
      "GET /api/v1/roles",
      json({
        items: ROLES.map(([key, name, derived]) => ({
          id: `r-${key}`,
          key,
          name,
          description: `${name} description`,
          is_derived: derived,
          permissions: [],
        })),
      }),
    );
}

/** A role's checkbox: its name followed by its description, so "HR" never matches "HR admin". */
function box(dialog: HTMLElement, name: string) {
  return within(dialog).getByRole("checkbox", { name: new RegExp(`^${name}\\s*${name} description`) });
}

async function openDialog() {
  const user = userEvent.setup();
  renderSignedIn(api, SUPER_ADMIN_ME, PATH);
  await user.click(await screen.findByRole("button", { name: "Add roles" }));
  return { user, dialog: await screen.findByRole("dialog") };
}

describe("adding roles", () => {
  it("adds several roles in one request and one step-up, keeping the roles held", async () => {
    setUp(["auditor"]);
    api
      .on(
        `POST /api/v1/users/${USER_ID}/roles/batch`,
        problem("step-up-required", 403, { max_age_seconds: 600 }),
      )
      .on(
        `POST /api/v1/users/${USER_ID}/roles/batch`,
        json({ items: [assignment("hr"), assignment("payroll_admin")] }, 201),
      )
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const { user, dialog } = await openDialog();
    // The role held is ticked and cannot be cleared here: the dialog only adds.
    const auditor = box(dialog, "Auditor");
    expect(auditor).toBeChecked();
    expect(auditor).toBeDisabled();
    expect(dialog).toHaveTextContent("Already assigned. It stays as it is.");
    // Derived roles are never offered.
    expect(within(dialog).queryByRole("checkbox", { name: /^Employee/ })).not.toBeInTheDocument();
    expect(await axe(dialog)).toHaveNoViolations();

    await user.click(box(dialog, "HR"));
    await user.click(box(dialog, "Payroll admin"));
    await user.type(within(dialog).getByLabelText("Reason"), "Covers HR and payroll");
    await user.click(within(dialog).getByRole("button", { name: "Add roles" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("2 roles assigned")).toBeInTheDocument();
    const calls = api.called(`POST /api/v1/users/${USER_ID}/roles/batch`);
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body).toEqual({
      roles: [{ role_key: "hr" }, { role_key: "payroll_admin" }],
      reason: "Covers HR and payroll",
    });
  });

  it("explains why data roles are not available for a super admin and shows the handover", async () => {
    setUp(["super_admin"]);
    const { dialog } = await openDialog();
    for (const name of ["HR", "HR admin", "Payroll admin", "System admin", "Auditor"]) {
      expect(box(dialog, name)).toBeDisabled();
    }
    expect(dialog).toHaveTextContent("A super admin gets this role only for a limited time");
    expect(screen.getByText(/To hand over this super admin role/)).toBeInTheDocument();
  });

  it("assigns super admin on its own, as a request for another super admin to approve", async () => {
    setUp([]);
    api.on(
      `POST /api/v1/users/${USER_ID}/roles`,
      json(
        {
          id: "g-1",
          kind: "super_admin_assignment",
          status: "pending",
          subject: { id: USER_ID, email: "role.target@dev.example" },
          role_key: "super_admin",
          department_id: null,
          location_id: null,
          reason: "Third admin",
          duration_minutes: null,
          requested_by: { id: SUPER_ADMIN_ME.user_id, email: "me@dev.example" },
          requested_at: "2026-01-01T00:00:00Z",
          decide_by: "2026-01-02T00:00:00Z",
          decided_by: null,
          decided_at: null,
          decision_note: null,
          starts_at: null,
          ends_at: null,
          revoked_by: null,
          revoked_at: null,
          acknowledged_by: null,
          acknowledged_at: null,
        },
        202,
      ),
    );
    const { user, dialog } = await openDialog();
    await user.click(box(dialog, "Super admin"));
    expect(box(dialog, "Auditor")).toBeDisabled();
    expect(dialog).toHaveTextContent("Another super admin, who is neither you nor this person");
    await user.type(within(dialog).getByLabelText("Reason"), "Third admin");
    await user.click(within(dialog).getByRole("button", { name: "Request super admin" }));
    expect(await screen.findByText(/another super admin must approve it/)).toBeInTheDocument();
    expect(api.called(`POST /api/v1/users/${USER_ID}/roles/batch`)).toHaveLength(0);
    expect(api.called(`POST /api/v1/users/${USER_ID}/roles`)[0]?.body).toEqual({
      role_key: "super_admin",
      reason: "Third admin",
    });
  });

  it("says nothing was changed when the batch is refused", async () => {
    setUp([]);
    api.on(
      `POST /api/v1/users/${USER_ID}/roles/batch`,
      problem("conflict", 409, {
        code: "role.already_assigned",
        detail: "This person already has this role.",
      }),
    );
    const { user, dialog } = await openDialog();
    await user.click(box(dialog, "Auditor"));
    await user.type(within(dialog).getByLabelText("Reason"), "Audit work");
    await user.click(within(dialog).getByRole("button", { name: "Add role" }));
    expect(await within(dialog).findByText(/This person already has this role/)).toBeInTheDocument();
  });

  it("is not offered without role.assign", async () => {
    setUp([]);
    renderSignedIn(api, HR_ADMIN_ME, PATH);
    expect(await screen.findByText("role.target@dev.example")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add roles" })).not.toBeInTheDocument();
  });
});
