import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

type Tone = "info" | "success" | "warning" | "danger";

const TONES: Record<Tone, { className: string; Icon: typeof Info }> = {
  info: { className: "bg-info-50 text-info-700", Icon: Info },
  success: { className: "bg-success-50 text-success-700", Icon: CircleCheck },
  warning: { className: "bg-warning-50 text-warning-700", Icon: TriangleAlert },
  danger: { className: "bg-danger-50 text-danger-700", Icon: CircleAlert },
};

/** A message with meaning in its icon and words, never colour alone. Danger is announced. */
export function Banner({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  const { className, Icon } = TONES[tone];
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={`flex items-start gap-2 rounded-[var(--radius-surface)] px-4 py-3 text-sm ${className}`}
    >
      <Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
      <div>{children}</div>
    </div>
  );
}
