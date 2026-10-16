import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { FakeApi, json, problem } from "../test/fakeApi";
import {
  DEPARTMENT,
  DESIGNATION,
  EMPLOYEE_ID,
  EMPLOYEE_ME,
  HR_ADMIN_ME,
  LOCATION,
  MANAGER_ME,
  employee,
  STATISTICS,
  summary,
} from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

function unit(item: { id: string; name: string }, extra: Record<string, unknown> = {}) {
  return {
    ...item,
    code: item.name.toUpperCase().replace(/\s/g, "-"),
    status: "active",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...extra,
  };
}

function orgLists() {
  api
    .on(
      "GET /api/v1/departments",
      json({ items: [unit(DEPARTMENT, { description: null, parent_id: null, head_employee_id: null })] }),
    )
    .on("GET /api/v1/designations", json({ items: [unit(DESIGNATION, { level: null })] }))
    .on(
      "GET /api/v1/locations",
      json({ items: [unit(LOCATION, { time_zone: "Asia/Kolkata", country_code: "IN", address: null })] }),
    );
}

describe("employees", () => {
  it("lists the records in scope with their placement and status", async () => {
    orgLists();
    api.on(
      "GET /api/v1/employees",
      json({
        items: [summary(), summary({ id: "e-2", name: "Test Leaver", status: "on_notice" })],
        next_cursor: null,
      }),
    );
    renderSignedIn(api, HR_ADMIN_ME, "/employees");
    const table = await screen.findByRole("table", { name: "Employees" });
    expect(within(table).getByRole("link", { name: /Test Person/ })).toHaveAttribute(
      "href",
      `/employees/${EMPLOYEE_ID}`,
    );
    expect(within(table).getByText("On notice")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add employee" })).toBeInTheDocument();
    expect(screen.getByText("End of the list")).toBeInTheDocument();
  });

  it("says what to do when there are no records yet", async () => {
    orgLists();
    api.on("GET /api/v1/employees", json({ items: [], next_cursor: null }));
    renderSignedIn(api, HR_ADMIN_ME, "/employees");
    expect(await screen.findByText("No employee records yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add the first employee" })).toBeInTheDocument();
  });

  it("checks the new employee form before sending it, and shows the API's field errors", async () => {
    orgLists();
    api.on("GET /api/v1/employees", json({ items: [], next_cursor: null })).on(
      "POST /api/v1/employees",
      problem("validation-error", 422, {
        errors: [
          {
            field: "employee_code",
            code: "employee.code_in_use",
            message: "Another employee already has this code.",
          },
        ],
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/employees/new");
    await user.click(await screen.findByRole("button", { name: "Add employee" }));
    expect(await screen.findByText("Enter the employee code.")).toBeInTheDocument();
    expect(screen.getByText("Choose a department.")).toBeInTheDocument();
    expect(api.called("POST /api/v1/employees")).toHaveLength(0);

    await user.type(screen.getByLabelText("Employee code"), "TEST-0001");
    await user.type(screen.getByLabelText("Legal first name"), "Test");
    await user.type(screen.getByLabelText("Date of joining"), "2026-10-01");
    for (const [label, option] of [
      ["Department", DEPARTMENT.name],
      ["Designation", DESIGNATION.name],
      ["Location", LOCATION.name],
      ["Employment type", "Full time"],
    ] as const) {
      await user.click(screen.getByRole("button", { name: new RegExp(`${label}$`) }));
      await user.click(await screen.findByRole("option", { name: option }));
    }
    await user.click(screen.getByRole("button", { name: "Add employee" }));
    expect(await screen.findByText("Another employee already has this code.")).toBeInTheDocument();
    expect(api.called("POST /api/v1/employees")[0]?.body).toMatchObject({
      employee_code: "TEST-0001",
      legal_first_name: "Test",
      legal_last_name: null,
      date_of_joining: "2026-10-01",
      job: { department_id: DEPARTMENT.id, employment_type: "full_time", manager_employee_id: null },
    });
  });

  it("shows a manager the team member's record without personal details or the account", async () => {
    api
      .on("GET /api/v1/employees/{employee_id}", json(employee()))
      .on(`GET /api/v1/employees/${EMPLOYEE_ID}`, json(employee()));
    renderSignedIn(api, MANAGER_ME, `/employees/${EMPLOYEE_ID}`);
    expect(await screen.findByRole("heading", { name: "Test Person" })).toBeInTheDocument();
    const tabs = screen.getByRole("tablist", { name: "Employee record" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual(["Overview", "Job history", "Status", "Attendance", "Leave"]);
    expect(screen.queryByRole("button", { name: "Edit details" })).not.toBeInTheDocument();
  });

  it("shows HR the personal tab and records a status change with a reason", async () => {
    api
      .on(`GET /api/v1/employees/${EMPLOYEE_ID}`, json(employee()))
      .on(`GET /api/v1/employees/${EMPLOYEE_ID}/status-changes`, json({ items: [] }))
      .on("GET /api/v1/users", json({ items: [], next_cursor: null }))
      .on(
        `POST /api/v1/employees/${EMPLOYEE_ID}/status-changes`,
        json(
          {
            id: "s-1",
            from_status: "active",
            to_status: "on_notice",
            effective_date: "2026-10-05",
            reason: "Test resignation",
            created_at: "2026-10-05T00:00:00Z",
          },
          201,
        ),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, `/employees/${EMPLOYEE_ID}`);
    const tabs = await screen.findByRole("tablist", { name: "Employee record" });
    expect(within(tabs).getByRole("tab", { name: "Personal" })).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: "HRMS account" })).toBeInTheDocument();
    await user.click(within(tabs).getByRole("tab", { name: "Status" }));
    await user.click(await screen.findByRole("button", { name: "Change status" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason"), "Test resignation");
    await user.click(within(dialog).getByRole("button", { name: "Change status" }));
    await waitFor(() => {
      expect(api.called(`POST /api/v1/employees/${EMPLOYEE_ID}/status-changes`)).toHaveLength(1);
    });
    expect(api.called(`POST /api/v1/employees/${EMPLOYEE_ID}/status-changes`)[0]?.body).toMatchObject({
      to_status: "on_notice",
      reason: "Test resignation",
    });
    expect(await screen.findByText("Status changed")).toBeInTheDocument();
  });

  it("says when a record is outside the people the viewer can see", async () => {
    api.on(`GET /api/v1/employees/${EMPLOYEE_ID}`, problem("not-found", 404));
    renderSignedIn(api, MANAGER_ME, `/employees/${EMPLOYEE_ID}`);
    expect(await screen.findByRole("heading", { name: "Employee not found" })).toBeInTheDocument();
  });
});

describe("self-service and the directory", () => {
  it("lets an employee change only their preferred name, sending the version they saw", async () => {
    api
      .on("GET /api/v1/me/profile", json(employee({ version: 3 })))
      .on("GET /api/v1/me/jobs", json({ items: [] }))
      .on(
        "GET /api/v1/me/personal",
        json({
          date_of_birth: null,
          personal_email: null,
          personal_phone: null,
          current_address: null,
          permanent_address: null,
          version: 0,
        }),
      )
      .on("PATCH /api/v1/me/profile", json(employee({ preferred_name: "Testy", version: 4 })));
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/profile");
    await user.click(await screen.findByRole("button", { name: "Change preferred name" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Preferred name"), "Testy");
    await user.click(within(dialog).getByRole("button", { name: "Save name" }));
    await waitFor(() => {
      expect(api.called("PATCH /api/v1/me/profile")).toHaveLength(1);
    });
    const [call] = api.called("PATCH /api/v1/me/profile");
    expect(call?.body).toEqual({ preferred_name: "Testy" });
    expect(call?.headers.get("If-Match")).toBe('"3"');
  });

  it("finds colleagues in the directory with directory fields only", async () => {
    orgLists();
    api.on(
      "GET /api/v1/directory",
      json({
        items: [
          {
            id: "e-3",
            name: "Test Colleague",
            preferred_name: null,
            work_email: "test.colleague@dev.example",
            department: DEPARTMENT,
            designation: DESIGNATION,
            location: LOCATION,
          },
        ],
        next_cursor: null,
      }),
    );
    const { container } = renderSignedIn(api, EMPLOYEE_ME, "/directory");
    expect(await screen.findByText("Test Colleague")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "test.colleague@dev.example" })).toHaveAttribute(
      "href",
      "mailto:test.colleague@dev.example",
    );
    // An employee without team or HR scope cannot open records, so names are not links.
    expect(screen.queryByRole("link", { name: "Test Colleague" })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("organization", () => {
  it("adds a department and confirms it", async () => {
    orgLists();
    api.on(
      "POST /api/v1/departments",
      json(
        unit(
          { id: "d-9", name: "Test new" },
          { description: "Test work", parent_id: null, head_employee_id: null },
        ),
        201,
      ),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization?tab=departments");
    await user.click(await screen.findByRole("button", { name: "Add department" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "NEW");
    await user.type(within(dialog).getByLabelText("Name"), "Test new");
    await user.type(within(dialog).getByLabelText("Description (optional)"), "Test work");
    await user.click(within(dialog).getByRole("button", { name: "Add department" }));
    expect(await screen.findByText("Department added")).toBeInTheDocument();
    expect(api.called("POST /api/v1/departments")[0]?.body).toEqual({
      code: "NEW",
      name: "Test new",
      description: "Test work",
      parent_id: null,
      head_employee_id: null,
    });
  });

  it.each([
    ["departments", "Add department", "No departments yet"],
    ["designations", "Add designation", "No designations yet"],
    ["locations", "Add location", "No locations yet"],
  ])("opens the add dialog when there are no %s yet", async (kind, button, empty) => {
    api
      .on("GET /api/v1/departments", json({ items: [] }))
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, `/organization?tab=${kind}`);
    expect(await screen.findByText(empty)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: button }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: button })).toBeInTheDocument();
  });

  it("adds the first location and shows the API's field errors", async () => {
    api
      .on("GET /api/v1/departments", json({ items: [] }))
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on(
        "POST /api/v1/locations",
        problem("validation-error", 422, {
          errors: [
            { field: "time_zone", code: "org.time_zone_unknown", message: "Enter a known time zone." },
          ],
        }),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization?tab=locations");
    await user.click(await screen.findByRole("button", { name: "Add location" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "TST");
    await user.type(within(dialog).getByLabelText("Name"), "Test office");
    await user.type(within(dialog).getByLabelText("Time zone"), "Nowhere/Test");
    await user.type(within(dialog).getByLabelText("Country code"), "in");
    await user.click(within(dialog).getByRole("button", { name: "Add location" }));
    expect(await within(dialog).findByText("Enter a known time zone.")).toBeInTheDocument();
    expect(api.called("POST /api/v1/locations")[0]?.body).toEqual({
      code: "TST",
      name: "Test office",
      time_zone: "Nowhere/Test",
      country_code: "IN",
      address: null,
    });
  });

  it("is read-only without org.manage", async () => {
    orgLists();
    renderSignedIn(api, EMPLOYEE_ME, "/organization?tab=departments");
    expect(await screen.findByRole("table", { name: "Departments" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add department" })).not.toBeInTheDocument();
  });
  it("shows set-up progress and headcounts, and opens a tab from a step", async () => {
    orgLists();
    api.on("GET /api/v1/employees/statistics", json(STATISTICS));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization");
    expect(await screen.findByRole("heading", { name: "Organization set up" })).toBeInTheDocument();
    const figures = screen.getByRole("group", { name: "Organization figures" });
    expect(await within(figures).findByText("Employed today")).toBeInTheDocument();
    const byDepartment = await screen.findByRole("list", { name: "Employees by department" });
    expect(within(byDepartment).getByRole("link", { name: "Test department" })).toHaveAttribute(
      "href",
      `/employees?department=${DEPARTMENT.id}`,
    );
    expect(within(byDepartment).getByText("42 people")).toBeInTheDocument();
    expect(await axe(document.body)).toHaveNoViolations();
    await user.click(screen.getByRole("button", { name: "Open locations" }));
    expect(await screen.findByRole("table", { name: "Locations" })).toBeInTheDocument();
  });

  it("makes each missing step actionable when nothing is set up", async () => {
    api
      .on("GET /api/v1/departments", json({ items: [] }))
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/employees/statistics", json({ ...STATISTICS, employed: 0 }));
    renderSignedIn(api, HR_ADMIN_ME, "/organization");
    expect(await screen.findByRole("heading", { name: "Set up your organization" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add locations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add departments" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add designations" })).toBeInTheDocument();
    expect(await screen.findByText("No one employed yet")).toBeInTheDocument();
  });

  it("filters a list by search text and shows the employee count per record", async () => {
    orgLists();
    api.on("GET /api/v1/employees/statistics", json(STATISTICS));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization?tab=departments");
    const table = await screen.findByRole("table", { name: "Departments" });
    expect(await within(table).findByRole("link", { name: "42" })).toHaveAttribute(
      "href",
      `/employees?department=${DEPARTMENT.id}`,
    );
    await user.type(screen.getByRole("searchbox", { name: "Search departments" }), "nothing like it");
    expect(await screen.findByText("No departments match this search")).toBeInTheDocument();
  });

  it("warns before archiving and explains why the API refused it", async () => {
    orgLists();
    api.on(
      `PATCH /api/v1/locations/${LOCATION.id}`,
      json(
        {
          type: "/problems/conflict",
          title: "Conflict",
          status: 409,
          detail:
            "3 employees are still placed in this location. Record a job change for them first, then archive it.",
          request_id: null,
          code: "org.in_use",
        },
        409,
      ),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization?tab=locations");
    await user.click(await screen.findByRole("button", { name: `Edit ${LOCATION.name}` }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByText(/Archiving hides this location/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Status/ }));
    await user.click(await screen.findByRole("option", { name: "Archived" }));
    expect(within(dialog).getByText(/Archiving hides this location/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive location" }));
    expect(await within(dialog).findByText(/3 employees are still placed/)).toBeInTheDocument();
    const [call] = api.called(`PATCH /api/v1/locations/${LOCATION.id}`);
    expect(call?.body).toMatchObject({ status: "archived" });
    expect(call?.headers.get("If-Match")).toBe('"1"');
  });
});

describe("employee list filters", () => {
  it("sends the designation filter and sort order and keeps them in the URL", async () => {
    orgLists();
    api.on("GET /api/v1/employees", json({ items: [summary()], next_cursor: null }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/employees?sort=joined_newest");
    await screen.findByRole("table", { name: "Employees" });
    await user.click(screen.getByRole("button", { name: /Designation/ }));
    await user.click(await screen.findByRole("option", { name: DESIGNATION.name }));
    await waitFor(() => {
      const last = api.called("GET /api/v1/employees").at(-1);
      expect(last?.query.get("designation_id")).toBe(DESIGNATION.id);
      expect(last?.query.get("sort")).toBe("joined_newest");
    });
  });
});

describe("linking an existing account", () => {
  function account(overrides: Record<string, unknown> = {}) {
    return {
      id: "01900000-0000-7000-8000-0000000000b1",
      email: "test.person@dev.example",
      status: "active",
      employee_id: null,
      locked: false,
      mfa_reenrolment_required: false,
      last_login_at: null,
      created_at: "2026-01-01T00:00:00Z",
      ...overrides,
    };
  }

  function usersFor(candidate: ReturnType<typeof account> | null) {
    api.on("GET /api/v1/users", (call) =>
      json({
        items: call.query.get("employee_id") || !candidate ? [] : [candidate],
        next_cursor: null,
      }),
    );
  }

  it("links the account that uses the work email, with a reason", async () => {
    const candidate = account();
    usersFor(candidate);
    api
      .on(`GET /api/v1/employees/${EMPLOYEE_ID}`, json(employee()))
      .on(
        `POST /api/v1/users/${candidate.id}/employee-link`,
        json({ ...candidate, employee_id: EMPLOYEE_ID }),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, `/employees/${EMPLOYEE_ID}`);
    await user.click(await screen.findByRole("tab", { name: "HRMS account" }));
    await user.click(await screen.findByRole("button", { name: "Link an existing account" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("test.person@dev.example", { selector: "dd *, dd" }),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Link account" }));
    expect(await within(dialog).findByText("Say why the account is linked now.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason"), "Test: invited before the record existed");
    await user.click(within(dialog).getByRole("button", { name: "Link account" }));
    await waitFor(() => {
      expect(api.called(`POST /api/v1/users/${candidate.id}/employee-link`)).toHaveLength(1);
    });
    expect(api.called(`POST /api/v1/users/${candidate.id}/employee-link`)[0]?.body).toEqual({
      employee_id: EMPLOYEE_ID,
      reason: "Test: invited before the record existed",
    });
  });

  it("says when the account belongs to another record, or when none exists", async () => {
    usersFor(account({ employee_id: "01900000-0000-7000-8000-0000000000e9" }));
    api.on(`GET /api/v1/employees/${EMPLOYEE_ID}`, json(employee()));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, `/employees/${EMPLOYEE_ID}`);
    await user.click(await screen.findByRole("tab", { name: "HRMS account" }));
    await user.click(await screen.findByRole("button", { name: "Link an existing account" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/already linked to another employee record/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Link account" })).not.toBeInTheDocument();
  });
});

describe("department heads and employment filters", () => {
  it("picks a head from the employee search and shows it in the list", async () => {
    api
      .on(
        "GET /api/v1/departments",
        json({
          items: [
            unit(DEPARTMENT, {
              description: null,
              parent_id: null,
              head_employee_id: EMPLOYEE_ID,
              head_name: "Test Person",
            }),
          ],
        }),
      )
      .on("GET /api/v1/designations", json({ items: [] }))
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/employees/statistics", json(STATISTICS))
      .on("GET /api/v1/employees", json({ items: [summary()], next_cursor: null }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization?tab=departments");
    const table = await screen.findByRole("table", { name: "Departments" });
    expect(within(table).getByText("Test Person")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: `Edit ${DEPARTMENT.name}` }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "Head (optional)" })).toHaveValue("Test Person");
  });

  it("lists people employed today by default and can show former employees", async () => {
    orgLists();
    api.on("GET /api/v1/employees", json({ items: [summary()], next_cursor: null }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/employees");
    await screen.findByRole("table", { name: "Employees" });
    expect(api.called("GET /api/v1/employees")[0]?.query.get("employment")).toBe("current");
    await user.click(screen.getByRole("button", { name: /Employment/ }));
    await user.click(await screen.findByRole("option", { name: "Former employees" }));
    await waitFor(() => {
      expect(api.called("GET /api/v1/employees").at(-1)?.query.get("employment")).toBe("former");
    });
  });
});
