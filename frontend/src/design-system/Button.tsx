import { LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Button as AriaButton, type ButtonProps as AriaButtonProps } from "react-aria-components";

type Variant = "primary" | "secondary" | "danger" | "quiet";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent-600 text-neutral-0 hover:bg-accent-700 pressed:bg-accent-700",
  secondary: "border border-neutral-300 bg-neutral-0 text-neutral-900 hover:bg-neutral-100",
  danger: "bg-danger-700 text-neutral-0 hover:opacity-90",
  quiet: "text-accent-700 hover:bg-accent-50",
};

export interface ButtonProps extends Omit<AriaButtonProps, "children" | "className"> {
  children: ReactNode;
  variant?: Variant;
}

/** Buttons say what happens ("Sign in", "Revoke session"). Pending shows an inline spinner. */
export function Button({ children, variant = "primary", isPending, ...props }: ButtonProps) {
  return (
    <AriaButton
      {...props}
      isPending={isPending ?? false}
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-[var(--radius-control)] px-4 text-sm font-semibold transition-colors duration-[var(--duration-fast)] disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]}`}
    >
      {isPending ? <LoaderCircle aria-hidden className="size-4 animate-spin" /> : null}
      {children}
    </AriaButton>
  );
}
