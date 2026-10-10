import { LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Button as AriaButton, type ButtonProps as AriaButtonProps } from "react-aria-components";
import { Link, type LinkProps } from "react-router";

type Variant = "primary" | "secondary" | "danger" | "quiet";
type Size = "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent-600 text-neutral-0 hover:bg-accent-700 pressed:bg-accent-800",
  secondary: "border border-neutral-300 bg-neutral-0 text-neutral-900 hover:bg-neutral-100",
  danger: "bg-danger-700 text-neutral-0 hover:opacity-90",
  quiet: "text-accent-700 hover:bg-accent-50",
};

const SIZES: Record<Size, string> = {
  sm: "min-h-8 px-3 text-sm",
  md: "min-h-10 px-4 text-sm",
};

function classes(variant: Variant, size: Size): string {
  return `inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)] font-semibold transition-colors duration-[var(--duration-fast)] disabled:cursor-not-allowed disabled:opacity-50 ${SIZES[size]} ${VARIANTS[variant]}`;
}

export interface ButtonProps extends Omit<AriaButtonProps, "children" | "className"> {
  children: ReactNode;
  variant?: Variant;
  size?: Size;
}

/** Buttons say what happens ("Sign in", "Revoke session"). Pending shows an inline spinner. */
export function Button({ children, variant = "primary", size = "md", isPending, ...props }: ButtonProps) {
  return (
    <AriaButton {...props} isPending={isPending ?? false} className={classes(variant, size)}>
      {isPending ? <LoaderCircle aria-hidden className="size-4 animate-spin" /> : null}
      {children}
    </AriaButton>
  );
}

/** A link that looks like a button, for actions that open another page. */
export function ButtonLink({
  children,
  variant = "primary",
  size = "md",
  ...props
}: LinkProps & { children: ReactNode; variant?: Variant; size?: Size }) {
  return (
    <Link {...props} className={classes(variant, size)}>
      {children}
    </Link>
  );
}
