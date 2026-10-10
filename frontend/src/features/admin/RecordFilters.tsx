/** Date range and text filters for the audit log and security events, applied on submit. */
import { useState } from "react";
import { Form } from "react-aria-components";

import { Button } from "../../design-system/Button";
import { TextField } from "../../design-system/TextField";

export interface RecordFilterValues {
  from: string;
  to: string;
  text: string;
}

export function RecordFilters({
  textLabel,
  textDescription,
  onApply,
}: {
  textLabel: string;
  textDescription: string;
  onApply: (values: RecordFilterValues) => void;
}) {
  const [values, setValues] = useState<RecordFilterValues>({ from: "", to: "", text: "" });
  const set = (key: keyof RecordFilterValues) => (value: string) => {
    setValues((current) => ({ ...current, [key]: value }));
  };
  return (
    <Form
      className="grid gap-3 rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-4 sm:grid-cols-[repeat(3,minmax(0,1fr))_auto] sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        onApply({ ...values, text: values.text.trim() });
      }}
    >
      <TextField label="From" name="from" type="date" value={values.from} onChange={set("from")} />
      <TextField label="To (inclusive)" name="to" type="date" value={values.to} onChange={set("to")} />
      <TextField
        label={textLabel}
        name="text"
        value={values.text}
        onChange={set("text")}
        maxLength={100}
        description={textDescription}
      />
      <Button type="submit" variant="secondary">
        Apply filters
      </Button>
    </Form>
  );
}

/** Calendar dates as the UTC instants the API filters on: from the start of `from` to the end of `to`. */
export function rangeQuery(values: RecordFilterValues): { from?: string; to?: string } {
  const query: { from?: string; to?: string } = {};
  if (values.from) query.from = new Date(`${values.from}T00:00:00`).toISOString();
  if (values.to) {
    const end = new Date(`${values.to}T00:00:00`);
    end.setDate(end.getDate() + 1);
    query.to = end.toISOString();
  }
  return query;
}
