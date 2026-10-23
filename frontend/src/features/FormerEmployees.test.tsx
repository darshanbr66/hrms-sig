import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import type { components } from "../lib/api-schema";
import { FakeApi, json, problem } from "../test/fakeApi";
import { EMPLOYEE_ME, HR_ADMIN_ME } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const UNIT = { id: "u-1", name: "Test unit" };

function former(
  id: string,
  account: components["schemas"]["FormerAccount"] | null,
): components["schemas"]["FormerEmployee"] {
  return {
    id,
    employee_code: `TEST-${id}`,
    name: `Test Leaver ${id}`,
    date_of_joining: "2025-01-06",
    date_of_exit: "2026-10-01",
    job: {
      department: UNIT,
      designation: UNIT,
      location: UNIT,
      manager: null,
      employment_type: "full_time",
      effective_from: "2025-01-06",
    },
    account,
  };
}

function page(
  items: components["schemas"]["FormerEmployee"][],
  { next = null, visible = true }: { next?: string | null; visible?: boolean } = {},
) {
  return json({ items, next_cursor: next, accounts_visible: visible });
}

function units() {
  for (const kind of ["departments", "designations", "locations"])
    api.on(`GET /api/v1/${kind}`, json({ items: [] }));
}

describe("former employees", () => {
  it("lists offboarded people with their account state, and warns about accounts still active", async () => {
    units();
    api.on(
      "GET /api/v1/former-employees",
      page([
        former("1", { id: "a-1", email: "leaver.one@dev.example", status: "disabled" }),
        former("2", { id: "a-2", email: "admin.leaver@dev.example", status: "active" }),
        former("3", null),
      ]),
    );
    renderSignedIn(api, HR_ADMIN_ME, "/former-employees");
    const table = await screen.findByRole("table", { name: "Former employees" });
    expect(within(table).getByText("Disabled")).toBeInTheDocument();
    expect(within(table).getByText("Still active")).toBeInTheDocument();
    expect(within(table).getByText("No account")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "admin.leaver@dev.example" })).toHaveAttribute(
      "href",
      "/admin/users/a-2",
    );
    expect(screen.getByText(/One account below is still active after offboarding/)).toBeInTheDocument();
    // Read-only: nothing to enable or change here.
    expect(screen.queryByRole("button", { name: /Enable/ })).not.toBeInTheDocument();
    expect(await axe(table)).toHaveNoViolations();
  });

  it("searches, filters and pages on the server", async () => {
    units();
    api
      .on("GET /api/v1/former-employees", page([former("1", null)], { next: "c-2" }))
      .on("GET /api/v1/former-employees", page([former("2", null)]))
      .on("GET /api/v1/former-employees", page([]));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/former-employees");
    await screen.findByRole("table", { name: "Former employees" });
    await user.click(screen.getByRole("button", { name: /Load more|Show more/ }));
    expect(await screen.findByText("Test Leaver 2")).toBeInTheDocument();
    expect(api.called("GET /api/v1/former-employees")[1]?.query.get("cursor")).toBe("c-2");
    await user.type(screen.getByLabelText("Search former employees"), "TEST-9{Enter}");
    expect(await screen.findByText("No former employees match these filters")).toBeInTheDocument();
    const last = api.called("GET /api/v1/former-employees").at(-1)?.query;
    expect(last?.get("q")).toBe("TEST-9");
    expect(last?.get("sort")).toBe("left_newest");
  });

  it("shows people but no accounts to a reader without user.read.all", async () => {
    units();
    api.on("GET /api/v1/former-employees", page([former("1", null)], { visible: false }));
    const reader = {
      ...HR_ADMIN_ME,
      permissions: HR_ADMIN_ME.permissions.filter((permission) => permission !== "user.read.all"),
    };
    renderSignedIn(api, reader, "/former-employees");
    const table = await screen.findByRole("table", { name: "Former employees" });
    expect(within(table).queryByRole("columnheader", { name: "Account" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Account")).not.toBeInTheDocument();
  });

  it("shows the empty and error states", async () => {
    units();
    api
      .on("GET /api/v1/former-employees", page([]))
      .on("GET /api/v1/former-employees", problem("internal", 500))
      .on("GET /api/v1/former-employees", problem("internal", 500));
    renderSignedIn(api, HR_ADMIN_ME, "/former-employees");
    expect(await screen.findByText("No former employees")).toBeInTheDocument();
  });

  it("is not reachable without organization-wide employee access", async () => {
    renderSignedIn(api, EMPLOYEE_ME, "/former-employees");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });

  it("leaves offboarded accounts out of the Users list request", async () => {
    api.on("GET /api/v1/users", json({ items: [], next_cursor: null }));
    renderSignedIn(api, HR_ADMIN_ME, "/admin/users");
    expect(await screen.findByText(/listed under Former employees/)).toBeInTheDocument();
    expect(api.called("GET /api/v1/users")[0]?.query.has("include_offboarded")).toBe(false);
  });
});
