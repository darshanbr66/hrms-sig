import { ChevronDown } from "lucide-react";
import {
  Button,
  FieldError,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Select as AriaSelect,
  SelectValue,
  Text,
} from "react-aria-components";

export interface SelectOption {
  id: string;
  label: string;
}

/** A labelled single choice from a short list. Label above, help text and error below. */
export function Select({
  label,
  name,
  options,
  value,
  onChange,
  description,
  error,
  placeholder,
  isDisabled = false,
}: {
  label: string;
  name: string;
  options: readonly SelectOption[];
  value: string | null;
  onChange: (value: string) => void;
  description?: string;
  error?: string | undefined;
  placeholder?: string;
  isDisabled?: boolean;
}) {
  return (
    <AriaSelect
      name={name}
      value={value}
      onChange={(key) => {
        if (key !== null) onChange(String(key));
      }}
      isInvalid={Boolean(error)}
      isDisabled={isDisabled}
      {...(placeholder ? { placeholder } : {})}
      validationBehavior="aria"
      className="flex flex-col gap-1"
    >
      <Label className="text-sm font-semibold text-neutral-800">{label}</Label>
      {description ? (
        <Text slot="description" className="text-sm text-neutral-600">
          {description}
        </Text>
      ) : null}
      <Button className="flex min-h-10 min-w-0 items-center justify-between gap-2 rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0 px-3 text-left text-base text-neutral-900">
        <SelectValue className="truncate placeholder-shown:text-neutral-500" />
        <ChevronDown aria-hidden className="size-4 shrink-0" />
      </Button>
      <FieldError className="text-sm text-danger-700">{error}</FieldError>
      <Popover className="max-h-72 min-w-[var(--trigger-width)] overflow-y-auto rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-1 shadow-[var(--shadow-float)]">
        <ListBox className="outline-none">
          {options.map((option) => (
            <ListBoxItem
              key={option.id}
              id={option.id}
              textValue={option.label}
              className="cursor-default rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-900 outline-none focus:bg-accent-50 selected:font-semibold"
            >
              {option.label}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </AriaSelect>
  );
}
