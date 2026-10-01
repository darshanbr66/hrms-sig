import type { Ref } from "react";
import { FieldError, Input, Label, Text, TextField as AriaTextField } from "react-aria-components";

export interface TextFieldProps {
  label: string;
  name: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  type?: "text" | "email" | "password";
  autoComplete?: string;
  description?: string;
  error?: string | undefined;
  inputMode?: "text" | "numeric";
  maxLength?: number;
  inputRef?: Ref<HTMLInputElement>;
}

/** Label above the field, help text below it, and the error linked to the input. */
export function TextField({
  label,
  name,
  value,
  onChange,
  onBlur,
  type = "text",
  autoComplete,
  description,
  error,
  inputMode,
  maxLength,
  inputRef,
}: TextFieldProps) {
  return (
    <AriaTextField
      name={name}
      value={value}
      onChange={onChange}
      {...(onBlur ? { onBlur } : {})}
      type={type}
      {...(autoComplete ? { autoComplete } : {})}
      {...(inputMode ? { inputMode } : {})}
      {...(maxLength ? { maxLength } : {})}
      isInvalid={Boolean(error)}
      // Forms validate with React Hook Form and the API; the browser must not block a resubmit.
      validationBehavior="aria"
      className="flex flex-col gap-1"
    >
      <Label className="text-sm font-semibold text-neutral-800">{label}</Label>
      {description ? (
        <Text slot="description" className="text-sm text-neutral-600">
          {description}
        </Text>
      ) : null}
      <Input
        {...(inputRef ? { ref: inputRef } : {})}
        className="min-h-10 rounded-[var(--radius-control)] border border-neutral-300 bg-neutral-0 px-3 text-base text-neutral-900 invalid:border-danger-700"
      />
      <FieldError className="text-sm text-danger-700">{error}</FieldError>
    </AriaTextField>
  );
}
