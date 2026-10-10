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
      json({ items: [unit(DEPARTMENT, { parent_id: null, head_employee_id: null })] }),
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
    ).toEqual(["Overview", "Job history", "Status", "Attendance"]);
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
      json(unit({ id: "d-9", name: "Test new" }, { parent_id: null, head_employee_id: null }), 201),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_ADMIN_ME, "/organization");
    await user.click(await screen.findByRole("button", { name: "Add department" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "NEW");
    await user.type(within(dialog).getByLabelText("Name"), "Test new");
    await user.click(within(dialog).getByRole("button", { name: "Add department" }));
    expect(await screen.findByText("Department added")).toBeInTheDocument();
    expect(api.called("POST /api/v1/departments")[0]?.body).toEqual({
      code: "NEW",
      name: "Test new",
      parent_id: null,
    });
  });

  it("is read-only without org.manage", async () => {
    orgLists();
    renderSignedIn(api, EMPLOYEE_ME, "/organization");
    expect(await screen.findByRole("table", { name: "Departments" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add department" })).not.toBeInTheDocument();
  });
});
