/**
 * A choice between two or three ways of showing the same records (a table or a month, a list
 * or a hierarchy). Built on React Aria's ToggleButtonGroup: one button is always chosen, the
 * group is labelled, and arrow keys move between the buttons.
 */
import type { LucideIcon } from "lucide-react";
import { ToggleButton, ToggleButtonGroup } from "react-aria-components";

// The chosen button is marked by a 2 px accent bar along its bottom edge, like the selected
// tab, not by colour alone: the pale fill and text colour differ from the other buttons by
// about 1.1:1, below WCAG 1.4.11's 3:1 for a state (e2e/field-contrast.e2e.ts).
const BUTTON =
  "flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-neutral-700 outline-none first:rounded-l-[var(--radius-control)] last:rounded-r-[var(--radius-control)] hover:bg-neutral-100 selected:bg-accent-50 selected:text-accent-700 selected:shadow-[inset_0_-2px_0_var(--color-accent-600)] data-[focus-visible]:outline-2 data-[focus-visible]:-outline-offset-2 data-[focus-visible]:outline-accent-600";

export function ViewSwitch<K extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { id: K; label: string; icon: LucideIcon }[];
  value: K;
  onChange: (value: K) => void;
}) {
  return (
    <ToggleButtonGroup
      aria-label={label}
      selectionMode="single"
      disallowEmptySelection
      selectedKeys={[value]}
      onSelectionChange={(keys) => {
        const chosen = options.find((option) => keys.has(option.id));
        if (chosen) onChange(chosen.id);
      }}
      className="flex self-start rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0"
    >
      {options.map(({ id, label: text, icon: Icon }) => (
        <ToggleButton key={id} id={id} className={BUTTON}>
          <Icon aria-hidden className="size-4" />
          {text}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
