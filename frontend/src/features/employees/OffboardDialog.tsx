/**
 * Offboarding: the one-step exit (product-requirements MVP item 11, `POST /employees/{id}/exit`).
 * Three steps in one dialog: the last working day and reason, a confirmation that names the
 * person and what will happen, then what was done. Nothing is deleted; the API keeps every
 * record and decides every check (permission, step-up, administrator accounts).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";

import { useStepUp } from "../../app/StepUp";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { DescriptionList, TextArea } from "../../design-system/Fields";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, localIsoDate } from "../../lib/format";
import { fieldMessages } from "../../lib/problem";
import { PEOPLE_KEY, type Employee } from "./queries";

type ExitResult = components["schemas"]["ExitView"];

const ACCOUNT_RESULT: Record<ExitResult["account"], (result: ExitResult) => string> = {
  none: () => "No HRMS account was linked to this record.",
  disabled: (result) =>
    `Their HRMS account is disabled. ${String(result.sessions_revoked)} active ${
      result.sessions_revoked === 1 ? "session was" : "sessions were"
    } ended and their remembered browsers were forgotten.`,
  already_disabled: () => "Their HRMS account was already disabled.",
  administrator_left_active: () =>
    "Their account holds an administrator role, so it is still active. Ask a system admin to disable it.",
};

export function OffboardDialog({ employee, onClose }: { employee: Employee; onClose: () => void }) {
  const queryClient = useQueryClient();
  const withStepUp = useStepUp();
  const [step, setStep] = useState<"details" | "confirm">("details");
  const [date, setDate] = useState(localIsoDate());
  const [reason, setReason] = useState("");
  const joined = employee.status === "pre_joining" ? "did not join" : "leaves";
  const offboard = useMutation({
    mutationFn: () =>
      withStepUp(() =>
        unwrap(
          client.POST("/api/v1/employees/{employee_id}/exit", {
            params: { path: { employee_id: employee.id } },
            body: { last_working_day: date, reason: reason.trim() },
          }),
        ),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
    onError: () => {
      setStep("details");
    },
  });
  const errors = fieldMessages(offboard.error);

  if (offboard.isSuccess) {
    return (
      <Dialog title={`${employee.name} is offboarded`} isOpen onOpenChange={onClose}>
        <div className="flex flex-col gap-3 text-sm text-neutral-700">
          <p>
            Recorded as left from {formatDate(offboard.data.status_change.effective_date, { weekday: false })}
            .
          </p>
          <p>{ACCOUNT_RESULT[offboard.data.account](offboard.data)}</p>
          <p>Their attendance, leave, job and status history and documents are kept.</p>
        </div>
        <DialogActions>
          <Button onPress={onClose}>Done</Button>
        </DialogActions>
      </Dialog>
    );
  }

  return (
    <Dialog title={`Offboard ${employee.name}`} isOpen onOpenChange={onClose}>
      {step === "details" ? (
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            setStep("confirm");
          }}
        >
          {offboard.isError && Object.keys(errors).length === 0 ? (
            <ProblemMessage error={offboard.error} fallback="We couldn't offboard this person. Try again." />
          ) : null}
          <TextField
            label="Last working day"
            name="last_working_day"
            type="date"
            value={date}
            onChange={setDate}
            max={localIsoDate()}
            description="Today or earlier. For a later date, mark them on notice from the Status tab."
            error={errors.last_working_day}
          />
          <TextArea
            label="Reason"
            name="reason"
            value={reason}
            onChange={setReason}
            maxLength={500}
            description="Kept in the status history and the audit log."
            error={errors.reason}
          />
          <DialogActions>
            <Button variant="secondary" onPress={onClose}>
              Cancel
            </Button>
            <Button type="submit" isDisabled={reason.trim() === "" || date === ""}>
              Review
            </Button>
          </DialogActions>
        </Form>
      ) : (
        <div className="flex flex-col gap-4 text-sm text-neutral-700">
          <DescriptionList
            items={[
              { label: "Employee", value: `${employee.name} (${employee.employee_code})` },
              { label: "Last working day", value: formatDate(date, { weekday: false }) },
              { label: "Reason", value: reason.trim() },
            ]}
          />
          <div>
            <p className="font-semibold text-neutral-900">
              When {employee.name} {joined}:
            </p>
            <ul className="mt-1 list-disc pl-5">
              <li>their record is marked as left and leaves the lists of current employees;</li>
              <li>their HRMS account is disabled and signed out everywhere;</li>
              <li>nothing is deleted: attendance, leave, history and documents are kept.</li>
            </ul>
          </div>
          <Banner tone="warning">
            This cannot be undone from the HRMS. You will be asked to confirm it&apos;s you.
          </Banner>
          <DialogActions>
            <Button
              variant="secondary"
              onPress={() => {
                setStep("details");
              }}
            >
              Back
            </Button>
            <Button
              variant="danger"
              isPending={offboard.isPending}
              onPress={() => {
                offboard.mutate();
              }}
            >
              Offboard {employee.name}
            </Button>
          </DialogActions>
        </div>
      )}
    </Dialog>
  );
}
