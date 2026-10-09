import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { createQueryClient, routes } from "../app/App";
import type { components } from "../lib/api-schema";
import { FakeApi, ME, json, problem } from "../test/fakeApi";

const TOKEN = "m".repeat(43);
const OTHER = "01900000-0000-7000-8000-000000000009";
let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const view = render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, ...view };
}

type GrantRequest = components["schemas"]["GrantRequestView"];

function grant(overrides: Partial<GrantRequest> = {}): GrantRequest {
  return {
    id: "req-1",
    kind: "elevation",
    status: "pending",
    subject: { id: OTHER, email: "other.admin@dev.example" },
    role_key: "payroll_admin",
    department_id: null,
    location_id: null,
    reason: "Fix a payroll configuration",
    duration_minutes: 120,
    requested_by: { id: OTHER, email: "other.admin@dev.example" },
    requested_at: "2030-01-01T09:00:00Z",
    decide_by: "2030-01-02T09:00:00Z",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    starts_at: null,
    ends_at: null,
    revoked_by: null,
    revoked_at: null,
    acknowledged_by: null,
    acknowledged_at: null,
    ...overrides,
  };
}

const SUPER_ADMIN = [
  "role.read",
  "role.assign",
  "role.elevation.request",
  "role.elevation.approve",
  "role.elevation.break_glass",
];

function signedIn(permissions: string[], scope: "full" | "mfa_enrolment" = "full") {
  api
    .on(
      "GET /api/v1/me",
      json({ ...ME, session_scope: scope, permissions: [...ME.permissions, ...permissions] }),
    )
    .on("GET /api/v1/me/elevation", json({ elevation: null }))
    .on(
      "GET /api/v1/roles",
      json({
        items: [
          { id: "r1", key: "employee", name: "Employee", description: "", is_derived: true, permissions: [] },
          {
            id: "r2",
            key: "payroll_admin",
            name: "Payroll admin",
            description: "",
            is_derived: false,
            permissions: [],
          },
          {
            id: "r3",
            key: "super_admin",
            name: "Super admin",
            description: "",
            is_derived: false,
            permissions: [],
          },
        ],
      }),
    );
}

describe("MFA re-enrolment", () => {
  it("needs the password, then sets up an authenticator and shows new recovery codes", async () => {
    api
      .on(
        `POST /api/v1/auth/mfa-reenrolment/${TOKEN}`,
        problem("validation-error", 422, {
          errors: [
            { field: "password", code: "password.incorrect", message: "Your password is not correct." },
          ],
        }),
      )
      .on(`POST /api/v1/auth/mfa-reenrolment/${TOKEN}`, json({ session_scope: "mfa_enrolment" }));
    signedIn([], "mfa_enrolment");
    api
      .on(
        "POST /api/v1/me/mfa/totp/setup",
        json({
          factor_id: "f-1",
          secret: "SECRET",
          otpauth_uri: "otpauth://x",
          qr_code: "data:image/svg+xml,x",
        }),
      )
      .on(
        "POST /api/v1/me/mfa/totp/confirm",
        json({ session_scope: "full", recovery_codes: ["aaaa-bbbb-cccc-dddd", "eeee-ffff-gggg-hhhh"] }),
      );
    const user = userEvent.setup();
    const { router } = renderAt(`/mfa-reenrolment/${TOKEN}`);
    await user.type(await screen.findByLabelText("Password"), "wrong password");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Your password is not correct.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "the right password");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText(/An administrator reset your authenticator app/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start setup" }));
    await user.type(await screen.findByLabelText("Enter the 6-digit code to confirm"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm authenticator" }));
    const codes = await screen.findByRole("list", { name: "Recovery codes" });
    expect(within(codes).getAllByRole("listitem")).toHaveLength(2);
    await user.click(screen.getByText("I have saved these codes"));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => {
      expect(router.state.location.pathname).not.toBe("/account/mfa-enrolment");
    });
  });

  it("says when the link no longer works", async () => {
    api.on(
      `POST /api/v1/auth/mfa-reenrolment/${TOKEN}`,
      problem("conflict", 409, { code: "mfa_reenrolment.invalid" }),
    );
    const user = userEvent.setup();
    renderAt(`/mfa-reenrolment/${TOKEN}`);
    await user.type(await screen.findByLabelText("Password"), "a password");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "This link doesn't work" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderAt(`/mfa-reenrolment/${TOKEN}`);
    await screen.findByRole("heading", { name: "Set up your authenticator app again" });
    expect(await axe(container, { rules: { "color-contrast": { enabled: false } } })).toHaveNoViolations();
  });
});

describe("access requests", () => {
  it("is not reachable without role.read", async () => {
    signedIn([]);
    renderAt("/admin/access-requests");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });

  it("shows requests read-only to an auditor", async () => {
    signedIn(["role.read"]);
    api.on("GET /api/v1/role-grant-requests", json({ items: [grant()], next_cursor: null }));
    renderAt("/admin/access-requests");
    expect(await screen.findByText("Fix a payroll configuration")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send request" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use break-glass" })).not.toBeInTheDocument();
  });

  it("approves another super admin's request after step-up", async () => {
    signedIn(SUPER_ADMIN);
    api
      .on("GET /api/v1/role-grant-requests", json({ items: [grant()], next_cursor: null }))
      .on("POST /api/v1/role-grant-requests/req-1/approve", problem("step-up-required", 403))
      .on("POST /api/v1/role-grant-requests/req-1/approve", json(grant({ status: "active" })))
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderAt("/admin/access-requests");
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Note (optional)"), "Checked with finance");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await user.type(await screen.findByLabelText("Code"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/role-grant-requests/req-1/approve")).toHaveLength(2);
    });
    expect(api.called("POST /api/v1/role-grant-requests/req-1/approve")[1]?.body).toEqual({
      note: "Checked with finance",
    });
  });

  it("offers no decision on one's own request, only withdrawing it", async () => {
    signedIn(SUPER_ADMIN);
    const mine = { id: ME.user_id, email: ME.email };
    api.on(
      "GET /api/v1/role-grant-requests",
      json({ items: [grant({ subject: mine, requested_by: mine })], next_cursor: null }),
    );
    renderAt("/admin/access-requests");
    expect(await screen.findByRole("button", { name: "End now" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("asks for temporary access with a role, a length and a reason", async () => {
    signedIn(SUPER_ADMIN);
    api
      .on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }))
      .on("POST /api/v1/role-grant-requests", json(grant(), 201));
    const user = userEvent.setup();
    renderAt("/admin/access-requests");
    expect(await screen.findByText("There are no access requests yet.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText("Choose a role.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Role/ }));
    expect(screen.queryByRole("option", { name: "Super admin" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Employee" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "Payroll admin" }));
    await user.type(screen.getByLabelText("Reason"), "Fix a payroll configuration");
    expect(screen.getByText(/you get the Payroll admin role for 2 hours/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/role-grant-requests")[0]?.body).toEqual({
        role_key: "payroll_admin",
        reason: "Fix a payroll configuration",
        duration_minutes: 120,
      });
    });
  });

  it("uses break-glass only with a reason", async () => {
    signedIn(SUPER_ADMIN);
    api
      .on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }))
      .on(
        "POST /api/v1/role-grant-requests/break-glass",
        json(grant({ kind: "break_glass", status: "active", role_key: "system_admin" }), 201),
      );
    const user = userEvent.setup();
    renderAt("/admin/access-requests");
    await user.click(await screen.findByRole("button", { name: "Use break-glass" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Use break-glass" }));
    expect(await within(dialog).findByText(/Say why no other super admin/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason"), "Both other admins are locked out");
    await user.click(within(dialog).getByRole("button", { name: "Use break-glass" }));
    await waitFor(() => {
      expect(api.called("POST /api/v1/role-grant-requests/break-glass")[0]?.body).toEqual({
        reason: "Both other admins are locked out",
        duration_minutes: 60,
      });
    });
  });

  it("shows temporary access in a banner while it lasts", async () => {
    api.on(
      "GET /api/v1/me/elevation",
      json({ elevation: grant({ status: "active", ends_at: "2030-01-01T11:00:00Z" }) }),
    );
    signedIn(["role.read"]);
    api.on("GET /api/v1/role-grant-requests", json({ items: [], next_cursor: null }));
    renderAt("/admin/access-requests");
    expect(await screen.findByText(/You have temporary payroll_admin access until/)).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    signedIn(SUPER_ADMIN);
    api.on("GET /api/v1/role-grant-requests", json({ items: [grant()], next_cursor: null }));
    const { container } = renderAt("/admin/access-requests");
    await screen.findByText("Fix a payroll configuration");
    expect(await axe(container, { rules: { "color-contrast": { enabled: false } } })).toHaveNoViolations();
  });
});
