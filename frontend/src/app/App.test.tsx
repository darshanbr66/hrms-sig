import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { refreshSession } from "../lib/api";
import { safeNext } from "../lib/navigation";
import { FakeApi, ME, json, problem } from "../test/fakeApi";
import { createQueryClient, routes } from "./App";
import { visibleNavItems } from "./AppShell";
import { RequirePermission } from "./guards";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function renderAt(path: string, routeList = routes) {
  const router = createMemoryRouter(routeList, { initialEntries: [path] });
  const view = render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, ...view };
}

function signedIn(me: object = ME) {
  api
    .on("GET /api/v1/me", json(me))
    .on("GET /api/v1/me/sessions", json({ items: [] }))
    .on("GET /api/v1/me/mfa/factors", json({ items: [] }))
    .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
}

async function signInWith(user: ReturnType<typeof userEvent.setup>, email = "person@dev.example") {
  await user.type(await screen.findByLabelText("Work email"), email);
  await user.type(screen.getByLabelText("Password"), "a long enough password");
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("protected routes", () => {
  it("sends a visitor without a session to sign-in, remembering where they were going", async () => {
    api
      .on("GET /api/v1/me", problem("unauthenticated", 401))
      .on("POST /api/v1/auth/refresh", problem("unauthenticated", 401));
    const { router } = renderAt("/account/security");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.search).toContain("next=%2Faccount%2Fsecurity");
  });

  it("shows the account page to a signed-in user, with only permitted navigation", async () => {
    signedIn();
    renderAt("/account/security");
    expect(await screen.findByRole("heading", { name: "Account security" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toHaveTextContent("Account security");
  });

  it("keeps an enrolment-only session on the enrolment screen", async () => {
    signedIn({ ...ME, session_scope: "mfa_enrolment", permissions: [] });
    renderAt("/account/security");
    expect(
      await screen.findByRole("heading", { name: "Set up a new authenticator app" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/signed in with a recovery code/)).toBeInTheDocument();
  });

  it("says so when a page needs a permission the user lacks", async () => {
    signedIn();
    const guarded = [
      { path: "/admin", element: <RequirePermission permission="user.read.all">Admin</RequirePermission> },
    ];
    renderAt("/admin", guarded);
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Admin")).not.toBeInTheDocument();
  });

  it("lists navigation items only for permissions held", () => {
    expect(visibleNavItems(ME).map((item) => item.label)).toEqual(["Account security"]);
  });
});

describe("sign-in", () => {
  it("signs in with a password, then a code, and lands on the account page", async () => {
    api
      .on("POST /api/v1/auth/login", json({ mfa_token: "t".repeat(43) }))
      .on("POST /api/v1/auth/login/mfa", json({ session_scope: "full" }));
    const user = userEvent.setup();
    const { router } = renderAt("/sign-in");
    await signInWith(user);
    await user.type(await screen.findByLabelText("6-digit code"), "123456");
    signedIn();
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Account security" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/account/security");
    const [login] = api.called("POST /api/v1/auth/login");
    expect(login?.headers.get("X-Requested-With")).toBe("sv-web");
    expect(api.called("POST /api/v1/auth/login/mfa")[0]?.body).toEqual({
      mfa_token: "t".repeat(43),
      code: "123456",
    });
  });

  it("shows one generic message for every credential failure", async () => {
    api.on("POST /api/v1/auth/login", problem("invalid-credentials", 401));
    const user = userEvent.setup();
    renderAt("/sign-in");
    await signInWith(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("The details you entered are not correct.");
  });

  it("explains rate limiting and an unavailable sign-in", async () => {
    api
      .on("POST /api/v1/auth/login", problem("rate-limited", 429, {}, { "Retry-After": "90" }))
      .on("POST /api/v1/auth/login", problem("temporarily-unavailable", 503, {}, { "Retry-After": "30" }));
    const user = userEvent.setup();
    renderAt("/sign-in");
    await signInWith(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Wait 2 minutes, then try again.");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Sign-in is temporarily unavailable.");
    });
  });

  it("checks the form before calling the API", async () => {
    const user = userEvent.setup();
    renderAt("/sign-in");
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Enter your work email.")).toBeInTheDocument();
    expect(api.calls.filter((call) => call.path === "/api/v1/auth/login")).toHaveLength(0);
  });

  it("returns to the first step when the sign-in challenge has expired", async () => {
    api
      .on("POST /api/v1/auth/login", json({ mfa_token: "t".repeat(43) }))
      .on(
        "POST /api/v1/auth/login/mfa",
        problem("invalid-credentials", 401, { code: "login.challenge_expired" }),
      );
    const user = userEvent.setup();
    renderAt("/sign-in");
    await signInWith(user);
    await user.type(await screen.findByLabelText("6-digit code"), "123456");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Your session ended. Sign in again to continue.")).toBeInTheDocument();
  });

  it("offers a recovery code and goes to enrolment after using one", async () => {
    api
      .on("POST /api/v1/auth/login", json({ mfa_token: "t".repeat(43) }))
      .on("POST /api/v1/auth/login/mfa", json({ session_scope: "mfa_enrolment" }));
    const user = userEvent.setup();
    const { router } = renderAt("/sign-in");
    await signInWith(user);
    await user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    await user.type(screen.getByLabelText("Recovery code"), "ABCD-EFGH-JKLM-NPQR");
    signedIn({ ...ME, session_scope: "mfa_enrolment", permissions: [] });
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/account/mfa-enrolment");
    });
    expect(api.called("POST /api/v1/auth/login/mfa")[0]?.body).toEqual({
      mfa_token: "t".repeat(43),
      recovery_code: "ABCD-EFGH-JKLM-NPQR",
    });
  });

  it("has no accessibility violations", async () => {
    const { container } = renderAt("/sign-in");
    await screen.findByRole("heading", { name: "Sign in" });
    // jsdom cannot compute colours; contrast is checked in a real browser (Playwright + axe).
    expect(await axe(container, { rules: { "color-contrast": { enabled: false } } })).toHaveNoViolations();
  });
});

describe("session lifecycle", () => {
  it("refreshes once on an expired access token and retries", async () => {
    api
      .on("GET /api/v1/me", problem("unauthenticated", 401))
      .on("GET /api/v1/me", json(ME))
      .on("POST /api/v1/auth/refresh", json({ session_scope: "full" }))
      .on("GET /api/v1/me/sessions", json({ items: [] }))
      .on("GET /api/v1/me/mfa/factors", json({ items: [] }))
      .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }));
    renderAt("/account/security");
    expect(await screen.findByRole("heading", { name: "Account security" })).toBeInTheDocument();
    expect(api.called("POST /api/v1/auth/refresh")).toHaveLength(1);
  });

  it("returns to sign-in when the session has ended", async () => {
    signedIn();
    const user = userEvent.setup();
    const { router } = renderAt("/account/security");
    await screen.findByRole("heading", { name: "Account security" });
    api
      .on("POST /api/v1/me/mfa/recovery-codes", problem("unauthenticated", 401))
      .on("POST /api/v1/auth/refresh", problem("unauthenticated", 401));
    await user.click(screen.getByRole("button", { name: "Make new recovery codes" }));
    expect(await screen.findByText("Your session ended. Sign in again to continue.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/sign-in");
  });

  it("asks only once for a refresh when requests fail together", async () => {
    let resolve: (response: Response) => void = () => undefined;
    api.on("POST /api/v1/auth/refresh", () => new Promise<Response>((done) => (resolve = done)));
    const first = refreshSession();
    const second = refreshSession();
    await vi.waitFor(() => {
      expect(api.called("POST /api/v1/auth/refresh")).toHaveLength(1);
    });
    resolve(json({ session_scope: "full" }));
    expect(await Promise.all([first, second])).toEqual([true, true]);
    expect(api.called("POST /api/v1/auth/refresh")).toHaveLength(1);
  });

  it("signs out and clears the session", async () => {
    signedIn();
    api.on("POST /api/v1/auth/logout", json(null, 204));
    const user = userEvent.setup();
    const { router } = renderAt("/account/security");
    await screen.findByRole("heading", { name: "Account security" });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/sign-in");
    });
    expect(api.called("POST /api/v1/auth/logout")).toHaveLength(1);
  });
});

describe("step-up", () => {
  it("asks for a code, verifies it and continues the original action", async () => {
    signedIn();
    api
      .on("POST /api/v1/me/mfa/recovery-codes", problem("step-up-required", 403, { max_age_seconds: 600 }))
      .on(
        "POST /api/v1/me/mfa/recovery-codes",
        json({ codes: ["AAAA-BBBB-CCCC-DDDD", "EEEE-FFFF-GGGG-HHHH"] }),
      )
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderAt("/account/security");
    await user.click(await screen.findByRole("button", { name: "Make new recovery codes" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(dialog).toHaveTextContent("authenticator app");
    expect(dialog).not.toHaveTextContent(/password/i);
    await user.type(screen.getByLabelText("Code"), "654321");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("list", { name: "Recovery codes" })).toHaveTextContent(
      "AAAA-BBBB-CCCC-DDDD",
    );
    expect(api.called("POST /api/v1/auth/step-up")[0]?.body).toEqual({ code: "654321" });
    expect(api.called("POST /api/v1/me/mfa/recovery-codes")).toHaveLength(2);
  });

  it("keeps the dialog open with a message when the code is wrong", async () => {
    signedIn();
    api.on("POST /api/v1/me/mfa/recovery-codes", problem("step-up-required", 403)).on(
      "POST /api/v1/auth/step-up",
      problem("validation-error", 422, {
        errors: [
          {
            field: "code",
            code: "mfa.invalid_code",
            message: "That code is not correct. Enter the current code.",
          },
        ],
      }),
    );
    const user = userEvent.setup();
    renderAt("/account/security");
    await user.click(await screen.findByRole("button", { name: "Make new recovery codes" }));
    await user.type(await screen.findByLabelText("Code"), "000000");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("That code is not correct. Enter the current code.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("redirect safety", () => {
  it.each([
    ["/account/security", "/account/security"],
    ["https://evil.example/", "/account/security"],
    ["//evil.example/account/", "/account/security"],
    ["/\\evil.example", "/account/security"],
    ["/admin/elsewhere", "/account/security"],
    [null, "/account/security"],
  ])("safeNext(%s) is %s", (next, expected) => {
    expect(safeNext(next)).toBe(expected);
  });
});
