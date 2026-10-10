/**
 * The personal tier (date of birth, personal contact, addresses): its own sub-resource with its
 * own permissions (CLAUDE.md rule 4). Used for the employee's own details (`/me/personal`) and
 * by HR on an employee's record, where every read is recorded in the audit log.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { Button } from "../../design-system/Button";
import { Dialog } from "../../design-system/Dialog";
import { DescriptionList } from "../../design-system/Fields";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate } from "../../lib/format";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { PEOPLE_KEY } from "./queries";

type Personal = components["schemas"]["PersonalView"];
type Address = components["schemas"]["Address"];

function addressText(address: Address | null): string | null {
  if (!address) return null;
  return [
    address.line1,
    address.line2,
    address.city,
    address.state,
    address.postal_code,
    address.country_code,
  ]
    .filter(Boolean)
    .join(", ");
}

/** `employeeId` null means the signed-in person's own details. */
export function PersonalDetails({ employeeId, canEdit }: { employeeId: string | null; canEdit: boolean }) {
  const key = [...PEOPLE_KEY, "personal", employeeId ?? "me"];
  const personal = useQuery({
    queryKey: key,
    queryFn: () =>
      employeeId
        ? unwrap(
            client.GET("/api/v1/employees/{employee_id}/personal", {
              params: { path: { employee_id: employeeId } },
            }),
          )
        : unwrap(client.GET("/api/v1/me/personal")),
  });
  const [editing, setEditing] = useState(false);
  return (
    <Panel
      title="Personal details"
      description={
        employeeId
          ? "Visible to HR only. Opening these details is recorded in the audit log."
          : "Visible to you and HR. Your manager cannot see them."
      }
      action={
        canEdit && personal.isSuccess ? (
          <Button
            variant="secondary"
            size="sm"
            onPress={() => {
              setEditing(true);
            }}
          >
            <Pencil aria-hidden className="size-3.5" />
            Edit
          </Button>
        ) : undefined
      }
    >
      {personal.isPending ? <Skeleton label="Loading personal details" /> : null}
      {personal.isError ? (
        <ProblemMessage
          error={personal.error}
          fallback="We couldn't load the personal details."
          onRetry={() => void personal.refetch()}
        />
      ) : null}
      {personal.isSuccess ? (
        <DescriptionList
          items={[
            {
              label: "Date of birth",
              value: personal.data.date_of_birth
                ? formatDate(personal.data.date_of_birth, { weekday: false })
                : null,
            },
            { label: "Personal email", value: personal.data.personal_email },
            { label: "Personal phone", value: personal.data.personal_phone },
            { label: "Current address", value: addressText(personal.data.current_address) },
            { label: "Permanent address", value: addressText(personal.data.permanent_address) },
          ]}
        />
      ) : null}
      {editing && personal.isSuccess ? (
        <PersonalDialog
          employeeId={employeeId}
          personal={personal.data}
          queryKey={key}
          onClose={() => {
            setEditing(false);
          }}
        />
      ) : null}
    </Panel>
  );
}

interface AddressDraft {
  line1: string;
  line2: string;
  city: string;
  state: string;
  postal_code: string;
  country_code: string;
}

function draftOf(address: Address | null): AddressDraft {
  return {
    line1: address?.line1 ?? "",
    line2: address?.line2 ?? "",
    city: address?.city ?? "",
    state: address?.state ?? "",
    postal_code: address?.postal_code ?? "",
    country_code: address?.country_code ?? "",
  };
}

function addressOf(draft: AddressDraft): Address | null {
  const parts = [draft.line1, draft.line2, draft.city, draft.state, draft.postal_code, draft.country_code];
  if (parts.every((value) => value.trim() === "")) return null;
  const optional = (value: string) => (value.trim() === "" ? null : value.trim());
  return {
    line1: draft.line1.trim(),
    line2: optional(draft.line2),
    city: draft.city.trim(),
    state: optional(draft.state),
    postal_code: optional(draft.postal_code),
    country_code: draft.country_code.trim().toUpperCase(),
  };
}

function PersonalDialog({
  employeeId,
  personal,
  queryKey,
  onClose,
}: {
  employeeId: string | null;
  personal: Personal;
  queryKey: readonly unknown[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [birth, setBirth] = useState(personal.date_of_birth ?? "");
  const [email, setEmail] = useState(personal.personal_email ?? "");
  const [phone, setPhone] = useState(personal.personal_phone ?? "");
  const [current, setCurrent] = useState(draftOf(personal.current_address));
  const [permanent, setPermanent] = useState(draftOf(personal.permanent_address));
  const body = {
    date_of_birth: birth === "" ? null : birth,
    personal_email: email.trim() === "" ? null : email.trim(),
    personal_phone: phone.trim() === "" ? null : phone.trim(),
    current_address: addressOf(current),
    permanent_address: addressOf(permanent),
  };
  const save = useMutation({
    mutationFn: () =>
      employeeId
        ? unwrap(
            client.PATCH("/api/v1/employees/{employee_id}/personal", {
              params: {
                path: { employee_id: employeeId },
                header: { "If-Match": ifMatch(personal.version) },
              },
              body,
            }),
          )
        : unwrap(
            client.PATCH("/api/v1/me/personal", {
              params: { header: { "If-Match": ifMatch(personal.version) } },
              body,
            }),
          ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey });
      toast("Personal details saved");
      onClose();
    },
  });
  const errors = fieldMessages(save.error);
  const hasFieldErrors = Object.keys(errors).length > 0;
  return (
    <Dialog title="Edit personal details" isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && !hasFieldErrors ? (
          <ProblemMessage error={save.error} fallback="We couldn't save the details. Try again." />
        ) : null}
        <div className="grid gap-4 sm:grid-cols-3">
          <TextField
            label="Date of birth"
            name="date_of_birth"
            type="date"
            value={birth}
            onChange={setBirth}
            error={errors.date_of_birth}
          />
          <TextField
            label="Personal email"
            name="personal_email"
            type="email"
            value={email}
            onChange={setEmail}
            maxLength={254}
            error={errors.personal_email}
          />
          <TextField
            label="Personal phone"
            name="personal_phone"
            type="tel"
            value={phone}
            onChange={setPhone}
            maxLength={32}
            error={errors.personal_phone}
          />
        </div>
        <AddressFields
          legend="Current address"
          draft={current}
          onChange={setCurrent}
          errors={errors}
          prefix="current_address"
        />
        <AddressFields
          legend="Permanent address"
          draft={permanent}
          onChange={setPermanent}
          errors={errors}
          prefix="permanent_address"
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Save details
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}

function AddressFields({
  legend,
  draft,
  onChange,
  errors,
  prefix,
}: {
  legend: string;
  draft: AddressDraft;
  onChange: (draft: AddressDraft) => void;
  errors: Record<string, string>;
  prefix: string;
}) {
  const field = (name: keyof AddressDraft, label: string, maxLength: number) => (
    <TextField
      label={label}
      name={`${prefix}.${name}`}
      value={draft[name]}
      onChange={(value) => {
        onChange({ ...draft, [name]: value });
      }}
      maxLength={maxLength}
      error={errors[`${prefix}.${name}`]}
    />
  );
  return (
    <fieldset className="flex flex-col gap-3">
      <legend className="mb-1 text-sm font-semibold text-neutral-900">{legend}</legend>
      <p className="text-xs text-neutral-600">Leave every field empty to remove this address.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {field("line1", "Address line 1", 200)}
        {field("line2", "Address line 2 (optional)", 200)}
        {field("city", "City", 100)}
        {field("state", "State (optional)", 100)}
        {field("postal_code", "Postal code (optional)", 20)}
        {field("country_code", "Country code", 2)}
      </div>
    </fieldset>
  );
}
