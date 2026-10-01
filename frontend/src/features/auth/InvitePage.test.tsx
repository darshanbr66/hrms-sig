import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { createQueryClient, routes } from "../../app/App";
import { FakeApi, ME, json, problem } from "../../test/fakeApi";

const TOKEN = "i".repeat(43);
const SETUP = {
  factor_id: "01900000-0000-7000-8000-0000000000aa",
  secret: "JBSWY3DPEHPK3PXP",
  otpauth_uri: "otpauth://totp/x",
  qr_code: "data:image/svg+xml;base64,PHN2Zy8+",
};

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function renderInvite() {
  const router = createMemoryRouter(routes, { initialEntries: [`/invite/${TOKEN}`] });
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("invite activation", () => {
  it("says plainly when a link no longer works", async () => {
    api.on(`GET /api/v1/auth/invite/${TOKEN}`, json({ valid: false, first_name: null }));
    renderInvite();
    expect(await screen.findByRole("heading", { name: "This invite link doesn't work" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Ask your HR team for a new invite.");
  });

  it("goes through password, authenticator and recovery codes, with no way to skip", async () => {
    api
      .on(`GET /api/v1/auth/invite/${TOKEN}`, json({ valid: true, first_name: "Asha" }))
      .on(
        `POST /api/v1/auth/invite/${TOKEN}/password`,
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
      .on(`POST /api/v1/auth/invite/${TOKEN}/password`, json({ enrolment_token: "e".repeat(43) }))
      .on(`POST /api/v1/auth/invite/${TOKEN}/mfa/totp/setup`, json(SETUP))
      .on(
        `POST /api/v1/auth/invite/${TOKEN}/mfa/totp/confirm`,
        json({ recovery_codes: ["AAAA-BBBB-CCCC-DDDD"] }),
      )
      .on("GET /api/v1/me", json(ME))
      .on("GET /api/v1/me/sessions", json({ items: [] }))
      .on("GET /api/v1/me/mfa/factors", json({ items: [] }))
      .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
    const user = userEvent.setup();
    const router = renderInvite();

    expect(await screen.findByRole("heading", { name: "Welcome, Asha" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /skip/i })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Password"), "first long passphrase");
    await user.type(screen.getByLabelText("Enter the password again"), "second long passphrase");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByText(/two passwords are different/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Enter the password again"));
    await user.type(screen.getByLabelText("Enter the password again"), "first long passphrase");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByText("This password has appeared in a data breach.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByRole("img", { name: /QR code/ })).toBeInTheDocument();
    expect(screen.getByText(SETUP.secret)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Enter the 6-digit code to confirm"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm authenticator" }));

    expect(await screen.findByRole("list", { name: "Recovery codes" })).toHaveTextContent(
      "AAAA-BBBB-CCCC-DDDD",
    );
    const finish = screen.getByRole("button", { name: "Go to your account" });
    expect(finish).toBeDisabled();
    await user.click(screen.getByText("I have saved these codes"));
    await user.click(finish);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/account/security");
    });
    const confirm = api.called(`POST /api/v1/auth/invite/${TOKEN}/mfa/totp/confirm`)[0];
    expect(confirm?.body).toEqual({
      enrolment_token: "e".repeat(43),
      factor_id: SETUP.factor_id,
      code: "123456",
    });
  });
});
