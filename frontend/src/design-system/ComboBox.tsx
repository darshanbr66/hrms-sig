import { ChevronDown } from "lucide-react";
import {
  Button,
  ComboBox as AriaComboBox,
  FieldError,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Text,
} from "react-aria-components";

export interface ComboOption {
  id: string;
  label: string;
  detail?: string;
}

/**
 * A searchable single choice, for long lists such as employees. The caller filters `options`
 * from `inputValue` (usually on the server); the box only shows them.
 */
export function ComboBox({
  label,
  options,
  selectedKey,
  onSelectionChange,
  inputValue,
  onInputChange,
  description,
  error,
  emptyText = "No matches.",
}: {
  label: string;
  options: readonly ComboOption[];
  selectedKey: string | null;
  onSelectionChange: (key: string | null) => void;
  inputValue: string;
  onInputChange: (value: string) => void;
  description?: string;
  error?: string | undefined;
  emptyText?: string;
}) {
  return (
    <AriaComboBox
      value={selectedKey}
      onChange={(key) => {
        onSelectionChange(key === null ? null : String(key));
      }}
      inputValue={inputValue}
      onInputChange={onInputChange}
      items={options}
      menuTrigger="focus"
      allowsEmptyCollection
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
      <div className="relative flex items-center">
        <Input className="min-h-10 w-full rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0 pl-3 pr-9 text-base text-neutral-900" />
        <Button className="absolute right-1 rounded p-1.5 text-neutral-600">
          <ChevronDown aria-hidden className="size-4" />
        </Button>
      </div>
      <FieldError className="text-sm text-danger-700">{error}</FieldError>
      <Popover
        data-scroll-area
        className="max-h-72 w-[var(--trigger-width)] overflow-y-auto overscroll-contain rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-1 shadow-[var(--shadow-float)]"
      >
        <ListBox
          className="outline-none"
          renderEmptyState={() => <p className="px-3 py-2 text-sm text-neutral-600">{emptyText}</p>}
        >
          {(option: ComboOption) => (
            <ListBoxItem
              id={option.id}
              textValue={option.label}
              className="cursor-default rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-900 outline-none focus:bg-accent-50 selected:font-semibold"
            >
              <span className="block">{option.label}</span>
              {option.detail ? <span className="block text-xs text-neutral-600">{option.detail}</span> : null}
            </ListBoxItem>
          )}
        </ListBox>
      </Popover>
    </AriaComboBox>
  );
}
