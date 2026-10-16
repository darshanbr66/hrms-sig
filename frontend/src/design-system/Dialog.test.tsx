import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { Button } from "./Button";
import { ConfirmDialog, Dialog, DialogActions } from "./Dialog";

function LongForm() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        onPress={() => {
          setOpen(true);
        }}
      >
        Add shift
      </Button>
      <Dialog title="Add shift" isOpen={open} onOpenChange={setOpen}>
        <form className="flex flex-col gap-4">
          {Array.from({ length: 30 }, (_, index) => (
            <label key={index}>
              Field {index + 1}
              <input name={`field-${index}`} />
            </label>
          ))}
          <DialogActions>
            <Button
              variant="secondary"
              onPress={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit">Add shift</Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}

describe("dialog layout", () => {
  it("keeps the title outside the scrolling body and pins the actions inside it", async () => {
    const user = userEvent.setup();
    render(<LongForm />);
    await user.click(screen.getByRole("button", { name: "Add shift" }));
    const dialog = await screen.findByRole("dialog", { name: "Add shift" });
    const body = dialog.querySelector("[data-dialog-body]");
    expect(body).not.toBeNull();
    expect(body).toHaveClass("overflow-y-auto", "min-h-0");
    // The title is not inside the scrolling body, so it never scrolls away.
    expect(body?.contains(within(dialog).getByRole("heading", { name: "Add shift" }))).toBe(false);
    const actions = dialog.querySelector("[data-dialog-actions]");
    expect(actions).toHaveClass("sticky", "bottom-0");
    expect(body?.contains(actions ?? null)).toBe(true);
    // The panel is capped at the overlay, which follows the visual viewport.
    expect(body?.closest(".max-h-full")).not.toBeNull();
    expect(document.querySelector('[class*="--visual-viewport-height"]')).not.toBeNull();
    expect(await axe(document.body)).toHaveNoViolations();
  });

  it("moves focus in, closes on Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    render(<LongForm />);
    const trigger = screen.getByRole("button", { name: "Add shift" });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Add shift" });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });
  });

  it("gives a confirmation the same capped layout with its buttons in the action row", () => {
    render(
      <ConfirmDialog
        title="Archive shift?"
        isOpen
        onOpenChange={() => undefined}
        confirmLabel="Archive"
        onConfirm={() => undefined}
      >
        The shift stays on past days.
      </ConfirmDialog>,
    );
    const dialog = screen.getByRole("alertdialog", { name: "Archive shift?" });
    const actions = dialog.querySelector("[data-dialog-actions]");
    expect(actions).not.toBeNull();
    expect(within(actions as HTMLElement).getByRole("button", { name: "Archive" })).toBeInTheDocument();
    expect(within(actions as HTMLElement).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });
});
