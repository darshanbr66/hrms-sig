import type { ReactNode } from "react";
import { Dialog as AriaDialog, Heading, Modal, ModalOverlay } from "react-aria-components";

/** A modal dialog. Focus moves in on open and returns to the trigger on close. */
export function Dialog({
  title,
  isOpen,
  onOpenChange,
  children,
}: {
  title: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      isDismissable
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 p-4"
    >
      <Modal className="w-full max-w-md rounded-[var(--radius-surface)] bg-neutral-0 p-6 shadow-lg">
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
