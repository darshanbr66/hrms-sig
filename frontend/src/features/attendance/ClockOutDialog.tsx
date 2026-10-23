/**
 * Correcting a forgotten clock-out (ATT-6, ATT-7; SOD-7): HR closes a session left open with
 * the time the person actually left, from verified information, and a reason. It is an
 * HR-recorded correction like any other: the clock-in and every recorded event stay as they
 * are, the clock-out is added as a correction event, the employee and their manager are told,
 * and the API checks the day and judges it again under the shift and attendance policy. Nothing
 * is assumed: no default time is offered, and a long day keeps its overtime.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Form } from "react-aria-components";

import { useStepUp } from "../../app/StepUp";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { Checkbox, DescriptionList, TextArea } from "../../design-system/Fields";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { addDays, formatDate, formatTime } from "../../lib/format";
import { fieldMessages, problemText } from "../../lib/problem";
import { zonedToIso } from "../../lib/zoned";
import { ATTENDANCE_KEY } from "./queries";

export interface OpenSession {
  employeeId: string;
  employeeName: string;
  /** The work date the open session belongs to. */
  workDate: string;
  timeZone: string;
  clockedInAt: string | null;
  onBreak: boolean;
}

export function ClockOutDialog({ session, onClose }: { session: OpenSession; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const withStepUp = useStepUp();
  const nextDay = addDays(session.workDate, 1);
  const [date, setDate] = useState(session.workDate);
  const [time, setTime] = useState("");
  const [breakEnded, setBreakEnded] = useState(false);
  const [breakEnd, setBreakEnd] = useState("");
  const [reason, setReason] = useState("");
  const [local, setLocal] = useState<string | null>(null);
  const idempotency = useRef(crypto.randomUUID());

  const save = useMutation({
    mutationFn: () => {
      const items = [
        ...(session.onBreak && breakEnded
          ? [
              {
                action: "add" as const,
                event_type: "BREAK_END" as const,
                proposed_at: zonedToIso(date, breakEnd, session.timeZone),
              },
            ]
          : []),
        {
          action: "add" as const,
          event_type: "CLOCK_OUT" as const,
          proposed_at: zonedToIso(date, time, session.timeZone),
        },
      ];
      return withStepUp(() =>
        unwrap(
          client.POST("/api/v1/employees/{employee_id}/attendance/corrections", {
            params: {
              path: { employee_id: session.employeeId },
              header: { "Idempotency-Key": idempotency.current },
            },
            body: { work_date: session.workDate, reason: reason.trim(), items },
          }),
        ),
      );
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
      toast("Clock-out corrected");
      onClose();
    },
    onError: () => {
      // A refused attempt can be fixed and sent again with a new key.
      idempotency.current = crypto.randomUUID();
    },
  });
  const errors = fieldMessages(save.error);

  return (
    <Dialog title={`Correct the clock-out for ${session.employeeName}`} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (time === "") setLocal("Enter the time they left.");
          else if (session.onBreak && breakEnded && breakEnd === "") setLocal("Enter when the break ended.");
          else if (reason.trim() === "") setLocal("Say how the departure time was verified.");
          else {
            setLocal(null);
            if (!save.isPending) save.mutate();
          }
        }}
      >
        <DescriptionList
          items={[
            { label: "Work date", value: formatDate(session.workDate, { weekday: true }) },
            {
              label: "Clocked in",
              value: session.clockedInAt ? formatTime(session.clockedInAt, session.timeZone) : "Not recorded",
            },
            { label: "Now", value: session.onBreak ? "On a break, never clocked out" : "Never clocked out" },
          ]}
        />
        <p className="text-sm text-neutral-700">
          Enter the time they actually left, from verified information such as the gate or access log. The
          recorded events stay as they are and the clock-out is added as a correction. Working time and
          overtime are worked out again from their shift and attendance policy; time after they left is not
          counted. Times are on the clock in {session.timeZone}.
        </p>
        {local ? <Banner tone="danger">{local}</Banner> : null}
        {save.isError ? (
          Object.keys(errors).length > 0 ? (
            <Banner tone="danger">{problemText(save.error, "Check the times and try again.")}</Banner>
          ) : (
            <ProblemMessage error={save.error} fallback="We couldn't correct the clock-out. Try again." />
          )
        ) : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <Select
            label="Left on"
            name="departure_date"
            options={[
              { id: session.workDate, label: formatDate(session.workDate, { weekday: true }) },
              { id: nextDay, label: `${formatDate(nextDay, { weekday: true })} (next day)` },
            ]}
            value={date}
            onChange={setDate}
          />
          <TextField label="Left at" name="departure_time" type="time" value={time} onChange={setTime} />
        </div>
        {session.onBreak ? (
          <div className="flex flex-col gap-3">
            <Checkbox isSelected={breakEnded} onChange={setBreakEnded}>
              Their break ended before they left
            </Checkbox>
            {breakEnded ? (
              <div className="max-w-xs">
                <TextField
                  label="Break ended at"
                  name="break_end_time"
                  type="time"
                  value={breakEnd}
                  onChange={setBreakEnd}
                  description="On the same day as the departure."
                />
              </div>
            ) : (
              <p className="text-sm text-neutral-600">
                Otherwise the break runs until they left and the day says the break&apos;s end was not
                recorded.
              </p>
            )}
          </div>
        ) : null}
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={1000}
          description="How the time was verified. Recorded with the correction and in the audit log; the employee and their manager are told."
          error={errors.reason}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Correct clock-out
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
