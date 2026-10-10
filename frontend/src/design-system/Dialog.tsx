import type { ReactNode } from "react";
import { Dialog as AriaDialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { Button } from "./Button";

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
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      isDismissable
      className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-neutral-900/40 p-4"
    >
      <Modal
        className={`w-full ${size === "lg" ? "max-w-2xl" : "max-w-md"} rounded-[var(--radius-surface)] bg-neutral-0 p-6 shadow-[var(--shadow-float)]`}
      >
        <AriaDialog className="outline-none">
          <Heading slot="title" className="text-[1.125rem] font-semibold text-neutral-900">
            {title}
          </Heading>
          <div className="mt-3">{children}</div>
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
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 p-4"
    >
      <Modal className="w-full max-w-md rounded-[var(--radius-surface)] bg-neutral-0 p-6 shadow-[var(--shadow-float)]">
        <AriaDialog role="alertdialog" className="outline-none">
          <Heading slot="title" className="text-[1.125rem] font-semibold text-neutral-900">
            {title}
          </Heading>
          <div className="mt-3 text-sm text-neutral-700">{children}</div>
          <div className="mt-6 flex flex-wrap justify-end gap-2">
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
          </div>
        </AriaDialog>
      </Modal>
    </ModalOverlay>
  );
}
