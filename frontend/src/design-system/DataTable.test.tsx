import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { DepartmentTree, type TreeDepartment } from "../features/organization/DepartmentTree";
import { DataTable } from "./DataTable";

const ROWS = [
  { id: "r-1", name: "First record" },
  { id: "r-2", name: "Second record" },
];

describe("DataTable details", () => {
  it("opens and closes a row's details with a named disclosure button, rendering them only while open", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <DataTable
        label="Records"
        columns={[{ key: "name", header: "Name", primary: true, cell: (row) => row.name }]}
        rows={ROWS}
        rowKey={(row) => row.id}
        detailsLabel={(row) => row.name}
        details={(row) => <p>Underlying events of {row.name}</p>}
      />,
    );
    expect(screen.queryByText(/Underlying events/)).not.toBeInTheDocument();
    const open = screen.getByRole("button", { name: "Show details for Second record" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    const close = screen.getByRole("button", { name: "Hide details for Second record" });
    expect(close).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(close.getAttribute("aria-controls") ?? "");
    expect(panel).toHaveTextContent("Underlying events of Second record");
    expect(screen.queryByText("Underlying events of First record")).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
    close.focus();
    await user.keyboard("{Enter}");
    expect(screen.queryByText(/Underlying events/)).not.toBeInTheDocument();
  });

  it("has no details column when no details are given", () => {
    render(
      <DataTable
        label="Records"
        columns={[{ key: "name", header: "Name", cell: (row) => row.name }]}
        rows={ROWS}
        rowKey={(row) => row.id}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(1);
  });
});

function department(id: string, name: string, parent: string | null): TreeDepartment {
  return { id, code: id, name, description: null, parent_id: parent, head_name: null, status: "active" };
}

describe("DepartmentTree", () => {
  it("renders children only when their parent is expanded and shows the chosen department", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <DepartmentTree
          departments={[
            department("a", "Test alpha", null),
            department("b", "Test beta", "a"),
            department("c", "Test gamma", "b"),
            // Its parent is not in the list (archived and hidden): shown at the top level.
            department("d", "Test delta", "gone"),
          ]}
          headcounts={new Map([["b", 4]])}
          onEdit={null}
          canListPeople={false}
        />
      </MemoryRouter>,
    );
    const tree = screen.getByRole("table", { name: "Department hierarchy" });
    expect(within(tree).getByRole("button", { name: "Test delta" })).toBeInTheDocument();
    expect(within(tree).queryByText("Test beta")).not.toBeInTheDocument();
    await user.click(within(tree).getByRole("button", { name: "Expand Test alpha, 1 sub-department" }));
    await user.click(within(tree).getByRole("button", { name: "Level 2: Test beta" }));
    const details = screen.getByRole("region", { name: "Selected department" });
    expect(details).toHaveTextContent("Test alpha");
    expect(details).toHaveTextContent("Test gamma");
    expect(details).toHaveTextContent("4");
    // Without access to the employee list, no link to it.
    expect(within(details).queryByRole("link")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand all" }));
    expect(within(tree).getByRole("button", { name: "Level 3: Test gamma" })).toBeInTheDocument();
  });

  it("cuts a loop instead of rendering forever", () => {
    render(
      <MemoryRouter>
        <DepartmentTree
          departments={[department("a", "Test alpha", "b"), department("b", "Test beta", "a")]}
          headcounts={null}
          onEdit={null}
          canListPeople={false}
        />
      </MemoryRouter>,
    );
    // Neither has a parent outside the loop, so nothing is at the top level; the page still renders.
    expect(screen.getByRole("table", { name: "Department hierarchy" })).toBeInTheDocument();
  });
});
