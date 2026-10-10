/**
 * Form fields beyond the single-line text field: multi-line text, search, a checkbox, and a
 * label/value list for reading records. Labels sit above fields; errors are linked to them.
 */
import { Check, Search, X } from "lucide-react";
import type { ReactNode } from "react";
import {
  Button as AriaButton,
  CheckboxButton,
  CheckboxField,
  FieldError,
  Input,
  Label,
  SearchField as AriaSearchField,
  Text,
  TextArea as AriaTextArea,
  TextField as AriaTextField,
} from "react-aria-components";

export function TextArea({
  label,
  name,
  value,
  onChange,
  description,
  error,
  maxLength,
  rows = 3,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (value: string) => void;
  description?: string;
  error?: string | undefined;
  maxLength?: number;
  rows?: number;
}) {
  return (
    <AriaTextField
      name={name}
      value={value}
      onChange={onChange}
      {...(maxLength ? { maxLength } : {})}
      isInvalid={Boolean(error)}
      validationBehavior="aria"
      className="flex flex-col gap-1"
    >
      <Label className="text-sm font-semibold text-neutral-800">{label}</Label>
      {description ? (
        <Text slot="description" className="text-sm text-neutral-600">
          {description}
        </Text>
      ) : null}
      <AriaTextArea
        rows={rows}
        className="rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0 px-3 py-2 text-base text-neutral-900"
      />
      <FieldError className="text-sm text-danger-700">{error}</FieldError>
    </AriaTextField>
  );
}

/** A search box. Submitting (Enter) or clearing reports the new text. */
export function SearchField({
  label,
  value,
  onChange,
  onSubmit,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit?: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <AriaSearchField
      value={value}
      onChange={onChange}
      {...(onSubmit ? { onSubmit } : {})}
      aria-label={label}
      className="group relative flex min-w-0 items-center"
    >
      <Search aria-hidden className="pointer-events-none absolute left-3 size-4 text-neutral-500" />
      <Input
        {...(placeholder ? { placeholder } : {})}
        className="min-h-10 w-full rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0 pl-9 pr-9 text-sm text-neutral-900 placeholder:text-neutral-500 [&::-webkit-search-cancel-button]:hidden"
      />
      <AriaButton
        aria-label="Clear search"
        className="absolute right-2 rounded p-1 text-neutral-500 hover:text-neutral-900 group-empty:hidden"
      >
        <X aria-hidden className="size-4" />
      </AriaButton>
    </AriaSearchField>
  );
}

export function Checkbox({
  children,
  isSelected,
  onChange,
}: {
  children: ReactNode;
  isSelected: boolean;
  onChange: (selected: boolean) => void;
}) {
  return (
    <CheckboxField isSelected={isSelected} onChange={onChange}>
      <CheckboxButton className="group flex items-start gap-2 text-sm text-neutral-800">
        <span
          aria-hidden
          className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border border-neutral-400 bg-neutral-0 group-selected:border-accent-600 group-selected:bg-accent-600"
        >
          <Check className="hidden size-3 text-neutral-0 group-selected:block" />
        </span>
        <span>{children}</span>
      </CheckboxButton>
    </CheckboxField>
  );
}

export function DescriptionList({ items }: { items: readonly { label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
      {items.map((item) => (
        <div key={item.label} className="min-w-0">
          <dt className="text-xs font-semibold text-neutral-500">{item.label}</dt>
          <dd className="mt-1 break-words text-sm text-neutral-900">{item.value ?? "Not recorded"}</dd>
        </div>
      ))}
    </dl>
  );
}
