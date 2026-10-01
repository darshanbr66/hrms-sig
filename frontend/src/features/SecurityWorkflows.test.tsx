import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { createQueryClient, routes } from "../app/App";
import { FakeApi, ME, json, problem } from "../test/fakeApi";

const TOKEN = "r".repeat(43);
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

function signedIn(permissions: string[] = []) {
  api
    .on("GET /api/v1/me", json({ ...ME, permissions: [...ME.permissions, ...permissions] }))
    .on("GET /api/v1/me/sessions", json({ items: [] }))
    .on("GET /api/v1/me/mfa/factors", json({ items: [] }))
    .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
}

describe("password reset", () => {
  it("says the same thing whatever the email", async () => {
    api.on("POST /api/v1/auth/password-reset", json(null, 202));
    const user = userEvent.setup();
    renderAt("/password-reset");
    await user.type(await screen.findByLabelText("Work email"), "person@dev.example");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("heading", { name: "Check your email" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("If an account uses that email");
    expect(api.called("POST /api/v1/auth/password-reset")[0]?.body).toEqual({ email: "person@dev.example" });
  });

  it("explains when requests are limited", async () => {
    api.on("POST /api/v1/auth/password-reset", problem("rate-limited", 429, {}, { "Retry-After": "3600" }));
    const user = userEvent.setup();
    renderAt("/password-reset");
    await user.type(await screen.findByLabelText("Work email"), "person@dev.example");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wait 60 minutes");
  });

  it("sets a new password and sends the person to sign in", async () => {
    api
      .on(
        `POST /api/v1/auth/password-reset/${TOKEN}`,
        problem("validation-error", 422, {
          errors: [
            {
              field: "password",
              code: "password.breached",
              message: "This password has appeared in a data breach.",
            },
          ],
        }),
      )
      .on(`POST /api/v1/auth/password-reset/${TOKEN}`, json(null, 204));
    const user = userEvent.setup();
    renderAt(`/password-reset/${TOKEN}`);
    await user.type(await screen.findByLabelText("New password"), "a long new passphrase");
    await user.type(screen.getByLabelText("Enter the new password again"), "a long new passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("This password has appeared in a data breach.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("heading", { name: "Your password is changed" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("authenticator app");
  });

  it("says when a link no longer works", async () => {
    api.on(
      `POST /api/v1/auth/password-reset/${TOKEN}`,
      problem("conflict", 409, { code: "password_reset.invalid" }),
    );
    const user = userEvent.setup();
    renderAt(`/password-reset/${TOKEN}`);
    await user.type(await screen.findByLabelText("New password"), "a long new passphrase");
    await user.type(screen.getByLabelText("Enter the new password again"), "a long new passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("heading", { name: "This link doesn't work" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ask for a new link" })).toHaveAttribute(
      "href",
      "/password-reset",
    );
  });

  it("has no accessibility violations", async () => {
    const { container } = renderAt("/password-reset");
    await screen.findByRole("heading", { name: "Reset your password" });
    expect(await axe(container, { rules: { "color-contrast": { enabled: false } } })).toHaveNoViolations();
  });
});

describe("recognized browsers", () => {
  it("lists browsers and forgets one", async () => {
    signedIn();
    api
      .on(
        "GET /api/v1/me/devices",
        json({
          items: [
            {
              id: "dev-1",
              current: true,
              user_agent: "Firefox on Linux",
              created_at: "2030-01-01T00:00:00Z",
              last_seen_at: "2030-01-02T00:00:00Z",
              expires_at: "2030-04-01T00:00:00Z",
            },
          ],
        }),
      )
      .on("DELETE /api/v1/me/devices/dev-1", json(null, 204));
    const user = userEvent.setup();
    renderAt("/account/security");
    expect(await screen.findByText("(this browser)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Forget this browser" }));
    await waitFor(() => {
      expect(api.called("DELETE /api/v1/me/devices/dev-1")).toHaveLength(1);
    });
  });
});

const SETTING = {
  key: "security.lockout.threshold",
  description: "Failed sign-in attempts within the window that lock an account",
  value: 10,
  default: 10,
  minimum: 3,
  maximum: 20,
  unit: "attempts",
  overridden: false,
  permission: "security.settings.manage",
};

describe("settings", () => {
  it("is not reachable without settings.read", async () => {
    signedIn();
    renderAt("/settings");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });

  it("shows values without controls to someone who may only read them", async () => {
    signedIn(["settings.read"]);
    api.on("GET /api/v1/settings", json({ items: [SETTING] }));
    renderAt("/settings");
    expect(await screen.findByText("Now 10 attempts (the default)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("changes a value after step-up", async () => {
    signedIn(["settings.read", "settings.manage", "security.settings.manage"]);
    api
      .on("GET /api/v1/settings", json({ items: [SETTING] }))
      .on("PUT /api/v1/settings/security.lockout.threshold", problem("step-up-required", 403))
      .on("PUT /api/v1/settings/security.lockout.threshold", json({ ...SETTING, value: 8, overridden: true }))
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderAt("/settings");
    const field = await screen.findByLabelText(/New value/);
    await user.clear(field);
    await user.type(field, "25");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter a whole number from 3 to 20.")).toBeInTheDocument();
    expect(api.called("PUT /api/v1/settings/security.lockout.threshold")).toHaveLength(0);
    await user.clear(field);
    await user.type(field, "8");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.type(await screen.findByLabelText("Code"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => {
      expect(api.called("PUT /api/v1/settings/security.lockout.threshold")).toHaveLength(2);
    });
    expect(api.called("PUT /api/v1/settings/security.lockout.threshold")[1]?.body).toEqual({ value: 8 });
  });
});
