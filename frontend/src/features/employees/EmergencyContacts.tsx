/** Emergency contacts (EMP-3): the personal tier, up to five in priority order, replaced as a
 * whole. Used for the employee's own contacts and by HR on an employee's record, where every
 * read is recorded in the audit log. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { Button } from "../../design-system/Button";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { problemText } from "../../lib/problem";
import { PEOPLE_KEY } from "./queries";

type Contact = components["schemas"]["EmergencyContactView"];
const MAX_CONTACTS = 5;

/** `employeeId` null means the signed-in person's own contacts. */
export function EmergencyContacts({ employeeId, canEdit }: { employeeId: string | null; canEdit: boolean }) {
  const key = [...PEOPLE_KEY, "emergency-contacts", employeeId ?? "me"];
  const contacts = useQuery({
    queryKey: key,
    queryFn: () =>
      employeeId
        ? unwrap(
            client.GET("/api/v1/employees/{employee_id}/emergency-contacts", {
              params: { path: { employee_id: employeeId } },
            }),
          )
        : unwrap(client.GET("/api/v1/me/emergency-contacts")),
  });
  const [editing, setEditing] = useState(false);
  return (
    <Panel
      title="Emergency contacts"
      description="Who to contact in an emergency, in order. Seen only by you and HR."
      action={
        canEdit && contacts.isSuccess ? (
          <Button
            size="sm"
            variant="secondary"
            onPress={() => {
              setEditing(true);
            }}
          >
            <Pencil aria-hidden className="size-4" />
            Edit contacts
          </Button>
        ) : undefined
      }
    >
      {contacts.isPending ? <Skeleton label="Loading emergency contacts" lines={2} /> : null}
      {contacts.isError ? (
        <ProblemMessage
          error={contacts.error}
          fallback="We couldn't load the emergency contacts."
          onRetry={() => void contacts.refetch()}
        />
      ) : null}
      {contacts.isSuccess && contacts.data.items.length === 0 ? (
        <p className="text-sm text-neutral-600">No emergency contacts recorded.</p>
      ) : null}
      {contacts.isSuccess && contacts.data.items.length > 0 ? (
        <ol aria-label="Emergency contacts" className="flex flex-col gap-2 text-sm">
          {contacts.data.items.map((contact) => (
            <li key={contact.priority}>
              <span className="font-semibold text-neutral-900">{contact.name}</span>{" "}
              <span className="text-neutral-600">
                ({contact.relationship}) · {contact.phone}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
      {editing && contacts.isSuccess ? (
        <ContactsDialog
          employeeId={employeeId}
          initial={contacts.data.items}
          onClose={() => {
            setEditing(false);
          }}
        />
      ) : null}
    </Panel>
  );
}

function ContactsDialog({
  employeeId,
  initial,
  onClose,
}: {
  employeeId: string | null;
  initial: Contact[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [rows, setRows] = useState(initial.map((contact, index) => ({ key: index, ...contact })));
  const save = useMutation({
    mutationFn: () => {
      const body = {
        contacts: rows.map(({ name, relationship, phone }) => ({
          name: name.trim(),
          relationship: relationship.trim(),
          phone: phone.trim(),
        })),
      };
      return employeeId
        ? unwrap(
            client.PUT("/api/v1/employees/{employee_id}/emergency-contacts", {
              params: { path: { employee_id: employeeId } },
              body,
            }),
          )
        : unwrap(client.PUT("/api/v1/me/emergency-contacts", { body }));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: [...PEOPLE_KEY, "emergency-contacts"] });
      toast("Emergency contacts saved");
      onClose();
    },
  });
  const update = (key: number, field: "name" | "relationship" | "phone", value: string) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
  };
  return (
    <Dialog title="Edit emergency contacts" isOpen onOpenChange={onClose} size="lg">
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        {save.isError ? (
          <ProblemMessage
            error={save.error}
            fallback={problemText(save.error, "We couldn't save the contacts.")}
          />
        ) : null}
        {rows.map((row, index) => (
          <fieldset key={row.key} className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end">
            <legend className="sr-only">Contact {index + 1}</legend>
            <TextField
              label={`Name ${index + 1}`}
              name={`name-${row.key}`}
              value={row.name}
              onChange={(value) => {
                update(row.key, "name", value);
              }}
              maxLength={200}
            />
            <TextField
              label={`Relationship ${index + 1}`}
              name={`relationship-${row.key}`}
              value={row.relationship}
              onChange={(value) => {
                update(row.key, "relationship", value);
              }}
              maxLength={100}
            />
            <TextField
              label={`Phone ${index + 1}`}
              name={`phone-${row.key}`}
              type="tel"
              value={row.phone}
              onChange={(value) => {
                update(row.key, "phone", value);
              }}
              maxLength={30}
            />
            <Button
              variant="quiet"
              size="sm"
              aria-label={`Remove contact ${index + 1}`}
              onPress={() => {
                setRows((current) => current.filter((item) => item.key !== row.key));
              }}
            >
              <Trash2 aria-hidden className="size-4" />
            </Button>
          </fieldset>
        ))}
        {rows.length < MAX_CONTACTS ? (
          <div>
            <Button
              size="sm"
              variant="secondary"
              onPress={() => {
                setRows((current) => [
                  ...current,
                  { key: Date.now(), priority: current.length + 1, name: "", relationship: "", phone: "" },
                ]);
              }}
            >
              <Plus aria-hidden className="size-4" />
              Add a contact
            </Button>
          </div>
        ) : null}
        <p className="text-sm text-neutral-600">
          The first contact is called first. A phone number may start with + and use spaces.
        </p>
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            isPending={save.isPending}
            isDisabled={rows.some((row) => !row.name.trim() || !row.relationship.trim() || !row.phone.trim())}
          >
            Save contacts
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
