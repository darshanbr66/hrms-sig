import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { components } from "../lib/api-schema";
import { FakeApi, json, problem } from "../test/fakeApi";
import { HR_ADMIN_ME, SUPER_ADMIN_ME } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const USER_ID = "01900000-0000-7000-8000-0000000000c1";
const PATH = `/admin/users/${USER_ID}`;

function account(
  overrides: Partial<components["schemas"]["UserView"]> = {},
): components["schemas"]["UserView"] {
  return {
    id: USER_ID,
    email: "test.disabled@dev.example",
    status: "disabled",
    employee_id: null,
    locked: false,
    mfa_reenrolment_required: false,
    last_login_at: null,
    created_at: "2026-01-01T00:00:00Z",
    enable_blocked: null,
    ...overrides,
  };
}

function setUp(view: components["schemas"]["UserView"]) {
  api
    .on(`GET /api/v1/users/${USER_ID}`, json(view))
    .on(`GET /api/v1/users/${USER_ID}/roles`, json({ items: [] }));
}

describe("enabling an account again", () => {
  it("enables an ordinarily disabled account after confirmation and step-up", async () => {
    setUp(account());
    api
      .on(`POST /api/v1/users/${USER_ID}/enable`, problem("step-up-required", 403, { max_age_seconds: 600 }))
      .on(`POST /api/v1/users/${USER_ID}/enable`, json(account({ status: "active" })))
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, PATH);
    await user.click(await screen.findByRole("button", { name: "Enable account" }));
    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent("They can sign in again");
    await user.click(within(confirm).getByRole("button", { name: "Enable account" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Account enabled")).toBeInTheDocument();
    expect(api.called(`POST /api/v1/users/${USER_ID}/enable`)).toHaveLength(2);
  });

  it("shows why a leaver's account stays disabled, with no Enable account action", async () => {
    setUp(account({ enable_blocked: "employee_exited", employee_id: "e-1" }));
    renderSignedIn(api, HR_ADMIN_ME, PATH);
    expect(
      await screen.findByText(/has been offboarded, so their account stays disabled/),
    ).toBeInTheDocument();
    expect(screen.getByText("Person has left")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable account" })).not.toBeInTheDocument();
  });

  it("shows that an erased account is disabled for good, with no Enable account action", async () => {
    setUp(account({ enable_blocked: "erased", email: "erased-0190@erased.invalid" }));
    renderSignedIn(api, HR_ADMIN_ME, PATH);
    expect(
      await screen.findByText(/disabled for good: it cannot be enabled, restored or linked/),
    ).toBeInTheDocument();
    expect(screen.getByText("Data erased")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable account" })).not.toBeInTheDocument();
  });

  it("shows the API's reason when the person left after the page was opened", async () => {
    setUp(account());
    api.on(
      `POST /api/v1/users/${USER_ID}/enable`,
      problem("conflict", 409, {
        code: "user.employee_exited",
        detail: "This person has left, so their account stays disabled. Their records stay as they are.",
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, PATH);
    await user.click(await screen.findByRole("button", { name: "Enable account" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "Enable account" }));
    expect(
      await within(confirm).findByText(/This person has left, so their account stays disabled/),
    ).toBeInTheDocument();
  });

  it("is not offered without user.disable", async () => {
    setUp(account());
    renderSignedIn(api, SUPER_ADMIN_ME, PATH);
    expect(await screen.findByText("test.disabled@dev.example", { selector: "h1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable account" })).not.toBeInTheDocument();
  });
});
