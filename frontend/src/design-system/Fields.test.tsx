import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { Checkbox } from "./Fields";
import { ToastProvider, useToast } from "./Toast";

function Controlled({ disabled = false }: { disabled?: boolean }) {
  const [on, setOn] = useState(false);
  return (
    <Checkbox isSelected={on} onChange={setOn} isDisabled={disabled}>
      Email me
    </Checkbox>
  );
}

describe("Checkbox", () => {
  it("toggles by click and by Space, and marks its state for the drawn box", async () => {
    const user = userEvent.setup();
    const { container } = render(<Controlled />);
    const box = screen.getByRole("checkbox", { name: "Email me" });
    const label = box.closest("label");
    await user.click(box);
    expect(box).toBeChecked();
    // The drawn box is styled from this attribute (`group-selected:`); without it nothing shows.
    expect(label).toHaveAttribute("data-selected", "true");
    await user.keyboard(" ");
    expect(box).not.toBeChecked();
    expect(label).not.toHaveAttribute("data-selected");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("cannot be changed while disabled", async () => {
    const user = userEvent.setup();
    render(<Controlled disabled />);
    const box = screen.getByRole("checkbox", { name: "Email me" });
    expect(box).toBeDisabled();
    await user.click(box);
    expect(box).not.toBeChecked();
  });
});

function Saver() {
  const toast = useToast();
  return (
    <>
      <button
        type="button"
        onClick={() => {
          toast("Preference saved");
        }}
      >
        Save
      </button>
      <button
        type="button"
        onClick={() => {
          toast("Holiday added");
        }}
      >
        Add
      </button>
    </>
  );
}

describe("Toast", () => {
  it("shows a repeated message once and restarts its time", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
      render(
        <ToastProvider>
          <Saver />
        </ToastProvider>,
      );
      for (let i = 0; i < 3; i += 1) await user.click(screen.getByRole("button", { name: "Save" }));
      await user.click(screen.getByRole("button", { name: "Add" }));
      expect(screen.getAllByText("Preference saved")).toHaveLength(1);
      expect(screen.getByText("Holiday added")).toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(4000);
      });
      await user.click(screen.getByRole("button", { name: "Save" }));
      act(() => {
        vi.advanceTimersByTime(4000);
      });
      // 8 s after the first save, but 4 s after the last: still showing.
      expect(screen.getByText("Preference saved")).toBeInTheDocument();
      expect(screen.queryByText("Holiday added")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
