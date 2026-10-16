import type { ReactNode } from "react";
import { Dialog as AriaDialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { Button } from "./Button";

/*
 * Every dialog fits the visible viewport, whatever its content: the overlay takes the visual
 * viewport's height (React Aria sets `--visual-viewport-height`, which follows zoom and the
 * on-screen keyboard), the panel is capped at that height, the title stays put and only the
 * body scrolls. Actions placed in `DialogActions` stay pinned to the bottom of the body while
 * it scrolls. Centring a taller-than-viewport panel with flexbox alone pushed its top out of
 * reach, which is why the panel is capped instead of the overlay scrolling.
 */
const OVERLAY =
  "fixed inset-x-0 top-0 z-50 flex h-[var(--visual-viewport-height,100dvh)] items-center justify-center bg-scrim/40 p-4";
const PANEL =
  "flex max-h-full w-full flex-col overflow-hidden rounded-[var(--radius-surface)] bg-neutral-0 shadow-[var(--shadow-float)]";
const HEADING = "shrink-0 px-6 pt-6 text-[1.125rem] font-semibold text-neutral-900";
const BODY = "min-h-0 overflow-y-auto overscroll-contain px-6 pt-3 pb-6 has-[[data-dialog-actions]]:pb-0";

/** The row of buttons that ends a dialog. Stays visible at the bottom while a long body scrolls. */
export function DialogActions({ children }: { children: ReactNode }) {
  return (
    <div
      data-dialog-actions
      className="sticky bottom-0 -mx-6 mt-2 flex flex-wrap justify-end gap-2 border-t border-neutral-200 bg-neutral-0 px-6 py-4"
    >
      {children}
    </div>
  );
}

/** A modal dialog. Focus moves in on open and returns to the trigger on close. */
export function Dialog({
  title,
  isOpen,
  onOpenChange,
  children,
  size = "md",
}: {
  title: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  size?: "md" | "lg";
}) {
  return (
    <ModalOverlay isOpen={isOpen} onOpenChange={onOpenChange} isDismissable className={OVERLAY}>
      <Modal className={`${PANEL} ${size === "lg" ? "max-w-2xl" : "max-w-md"}`}>
        <AriaDialog className="flex min-h-0 flex-col outline-none">
          <Heading slot="title" className={HEADING}>
            {title}
          </Heading>
          <div data-dialog-body data-scroll-area className={BODY}>
            {children}
          </div>
        </AriaDialog>
      </Modal>
    </ModalOverlay>
  );
}

/**
 * Confirmation for a consequential action (ui-ux-guidelines.md §6, Confirmations): it names
 * the object and the consequence, and the confirming button says what will happen.
 */
export function ConfirmDialog({
  title,
  isOpen,
  onOpenChange,
  confirmLabel,
  onConfirm,
  isPending = false,
  tone = "danger",
  children,
}: {
  title: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  confirmLabel: string;
  onConfirm: () => void;
  isPending?: boolean;
  tone?: "danger" | "primary";
  children: ReactNode;
}) {
  return (
    <ModalOverlay isOpen={isOpen} onOpenChange={onOpenChange} className={OVERLAY}>
      <Modal className={`${PANEL} max-w-md`}>
        <AriaDialog role="alertdialog" className="flex min-h-0 flex-col outline-none">
          <Heading slot="title" className={HEADING}>
            {title}
          </Heading>
          <div data-dialog-body data-scroll-area className={`${BODY} text-sm text-neutral-700`}>
            {children}
            <DialogActions>
              <Button
                variant="secondary"
                onPress={() => {
                  onOpenChange(false);
                }}
              >
                Cancel
              </Button>
              <Button variant={tone} onPress={onConfirm} isPending={isPending}>
                {confirmLabel}
              </Button>
            </DialogActions>
          </div>
        </AriaDialog>
      </Modal>
    </ModalOverlay>
  );
}
