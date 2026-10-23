import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import type { components } from "../lib/api-schema";
import { FakeApi, json, problem } from "../test/fakeApi";
import { HR_ADMIN_ME, SUPER_ADMIN_ME } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

const ME = {
  ...SUPER_ADMIN_ME,
  permissions: [
    ...SUPER_ADMIN_ME.permissions,
    "employee.erasure.read",
    "employee.erasure.request",
    "employee.erasure.approve",
  ],
};
const OTHER = { id: "u-other", email: "other.admin@dev.example" };

function erasure(
  overrides: Partial<components["schemas"]["ErasureView"]> = {},
): components["schemas"]["ErasureView"] {
  return {
    id: "er-1",
    status: "pending",
    employee: { id: "e-1", name: "Test Leaver", employee_code: "TEST-0042" },
    reason: "Test: authorized by the data protection officer",
    requested_by: OTHER,
    requested_at: "2026-10-20T10:00:00Z",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    result: null,
    files_total: 0,
    files_removed: 0,
    files_remaining: 0,
    last_file_error: null,
    completed_at: null,
    ...overrides,
  };
}

const RESULT: components["schemas"]["ErasureCategoryView"][] = [
  { key: "people.personal_details", outcome: "deleted", count: 1, note: "Date of birth and addresses." },
  { key: "people.employee_record", outcome: "anonymized", count: 1, note: "Names replaced by placeholders." },
  { key: "attendance.events", outcome: "retained", count: 12, note: "Clock events: append-only history." },
  { key: "documents.files", outcome: "pending", count: 2, note: "The stored files of every version." },
];

describe("data erasure", () => {
  it("asks for an erasure only with the code, a reason and an explicit confirmation", async () => {
    api
      .on("GET /api/v1/employee-erasures", json({ items: [] }))
      .on("POST /api/v1/employee-erasures", problem("step-up-required", 403, { max_age_seconds: 600 }))
      .on(
        "POST /api/v1/employee-erasures",
        json(erasure({ requested_by: { id: ME.user_id, email: "me@dev.example" } }), 201),
      )
      .on("POST /api/v1/auth/step-up", json({ step_up_expires_at: "2030-01-01T00:10:00Z" }));
    const user = userEvent.setup();
    renderSignedIn(api, ME, "/admin/erasures");
    await user.click(await screen.findByRole("button", { name: "Permanently delete employee data" }));
    const dialog = await screen.findByRole("dialog", { name: "Permanently delete employee data" });
    expect(dialog).toHaveTextContent("Nothing is deleted yet");
    expect(dialog).toHaveTextContent("Kept under the record ID");
    expect(await axe(dialog)).toHaveNoViolations();

    await user.click(within(dialog).getByRole("button", { name: "Ask for erasure" }));
    expect(within(dialog).getByText("Enter the person's employee code.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Employee code"), "TEST-0042");
    await user.type(within(dialog).getByLabelText("Reason"), "Authorized by the DPO");
    await user.click(within(dialog).getByRole("button", { name: "Ask for erasure" }));
    expect(within(dialog).getByText(/Confirm that you understand/)).toBeInTheDocument();
    expect(api.called("POST /api/v1/employee-erasures")).toHaveLength(0);

    await user.click(within(dialog).getByRole("checkbox", { name: /cannot be undone/ }));
    await user.click(within(dialog).getByRole("button", { name: "Ask for erasure" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/another super admin must approve it/)).toBeInTheDocument();
    expect(api.called("POST /api/v1/employee-erasures")[1]?.body).toEqual({
      employee_code: "TEST-0042",
      reason: "Authorized by the DPO",
    });
  });

  it("lets a different super admin approve with the code typed again, and reports what remains", async () => {
    api.on("GET /api/v1/employee-erasures", json({ items: [erasure()] })).on(
      "POST /api/v1/employee-erasures/er-1/approve",
      json(
        erasure({
          status: "files_pending",
          decided_by: { id: ME.user_id, email: "me@dev.example" },
          decided_at: "2026-10-23T10:00:00Z",
          employee: { id: "e-1", name: "Erased employee", employee_code: "ERASED-01" },
          result: RESULT,
          files_total: 2,
          files_remaining: 2,
        }),
      ),
    );
    const user = userEvent.setup();
    renderSignedIn(api, ME, "/admin/erasures");
    const table = await screen.findByRole("table", { name: "Erasure requests" });
    expect(within(table).getByText("Waiting for a second super admin")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: /Open/ }));
    const dialog = await screen.findByRole("dialog", { name: /Erasure of Test Leaver's data/ });
    await user.type(within(dialog).getByLabelText("Type the employee code to confirm"), "TEST-0042");
    await user.click(within(dialog).getByRole("checkbox", { name: /cannot be undone/ }));
    await user.click(within(dialog).getByRole("button", { name: "Approve and delete data" }));
    expect(await screen.findByText("Records erased; the stored files are being removed")).toBeInTheDocument();
    expect(api.called("POST /api/v1/employee-erasures/er-1/approve")[0]?.body).toEqual({
      employee_code: "TEST-0042",
    });
  });

  it("never reads as fully deleted: it lists what is kept and what is still being removed", async () => {
    api.on(
      "GET /api/v1/employee-erasures",
      json({
        items: [
          erasure({
            status: "files_pending",
            decided_by: OTHER,
            decided_at: "2026-10-23T10:00:00Z",
            result: RESULT,
            files_total: 2,
            files_remaining: 1,
            last_file_error: "FileStoreError",
          }),
        ],
      }),
    );
    const user = userEvent.setup();
    renderSignedIn(api, ME, "/admin/erasures");
    const rows = await screen.findByRole("table", { name: "Erasure requests" });
    await user.click(within(rows).getByRole("button", { name: /Open/ }));
    const dialog = await screen.findByRole("dialog", { name: /Erasure of Test Leaver/ });
    expect(dialog).toHaveTextContent(
      "1 of 2 stored files are still being removed, so this request is not complete yet",
    );
    expect(dialog).toHaveTextContent("The last attempt failed");
    const result = within(dialog).getByRole("table", { name: "What happened to each kind of record" });
    expect(within(result).getByText("Kept")).toBeInTheDocument();
    expect(within(result).getByText("Being removed")).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent(/all (of the |personal )?data (was|has been) deleted/i);
    // A decided request offers no approval.
    expect(within(dialog).queryByRole("button", { name: "Approve and delete data" })).not.toBeInTheDocument();
  });

  it("does not offer approval of one's own request, only withdrawal", async () => {
    api
      .on(
        "GET /api/v1/employee-erasures",
        json({ items: [erasure({ requested_by: { id: ME.user_id, email: "me@dev.example" } })] }),
      )
      .on("POST /api/v1/employee-erasures/er-1/cancel", json(erasure({ status: "cancelled" })));
    const user = userEvent.setup();
    renderSignedIn(api, ME, "/admin/erasures");
    const rows = await screen.findByRole("table", { name: "Erasure requests" });
    await user.click(within(rows).getByRole("button", { name: /Open/ }));
    const dialog = await screen.findByRole("dialog", { name: /Erasure of Test Leaver/ });
    expect(within(dialog).queryByRole("button", { name: "Approve and delete data" })).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("A different super admin must approve your request.");
    await user.click(within(dialog).getByRole("button", { name: "Withdraw request" }));
    expect(await screen.findByText("Erasure request withdrawn")).toBeInTheDocument();
  });

  it("is not reachable without the erasure permissions", async () => {
    renderSignedIn(api, HR_ADMIN_ME, "/admin/erasures");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });
});
