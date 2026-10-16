/**
 * Asking for (or, for HR, recording) a correction to one work date (ATT-7, ATT-8): remove
 * events that are wrong and add the ones that are missing, with a reason. Times are entered on
 * the clock of the employee's location. The API checks the resulting day; nothing is changed
 * until a manager or HR approves an employee's request.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { Form } from "react-aria-components";
import { useNavigate } from "react-router";

import { useStepUp } from "../../app/StepUp";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { Checkbox, TextArea } from "../../design-system/Fields";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDate, formatTime, localIsoDate } from "../../lib/format";
import { fieldMessages, problemText } from "../../lib/problem";
import { addDays } from "../../lib/format";
import { zonedToIso } from "../../lib/zoned";
import { eventLabel, type Day } from "./DaysTable";
import { ATTENDANCE_KEY, type EventType } from "./queries";

const EVENT_OPTIONS: { id: EventType; label: string }[] = [
  { id: "CLOCK_IN", label: "Clock in" },
  { id: "BREAK_START", label: "Break start" },
  { id: "BREAK_END", label: "Break end" },
  { id: "CLOCK_OUT", label: "Clock out" },
];

interface Added {
  key: number;
  type: EventType | null;
  time: string;
  nextDay: boolean;
}

export function CorrectionDialog({
  day,
  workDate,
  timeZone,
  employeeId,
  onClose,
}: {
  /** The day as recorded, or null for a date with nothing listed. */
  day: Day | null;
  workDate: string | null;
  timeZone: string;
  /** Set when HR records the correction for someone else (applies at once). */
  employeeId?: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const withStepUp = useStepUp();
  const [date, setDate] = useState(workDate ?? addDays(localIsoDate(), -1));
  const [voids, setVoids] = useState<string[]>([]);
  const [added, setAdded] = useState<Added[]>([{ key: 1, type: null, time: "", nextDay: false }]);
  const [reason, setReason] = useState("");
  const [local, setLocal] = useState<string | null>(null);
  const idempotency = useRef(crypto.randomUUID());
  const events = (day?.events ?? []).filter((event) => !event.voided);
  const hr = employeeId !== undefined;

  const save = useMutation({
    mutationFn: () => {
      const items = [
        ...voids.map((id) => ({ action: "void" as const, target_event_id: id })),
        ...added
          .filter((item) => item.type && item.time)
          .map((item) => ({
            action: "add" as const,
            event_type: item.type as EventType,
            proposed_at: zonedToIso(item.nextDay ? addDays(date, 1) : date, item.time, timeZone),
          })),
      ];
      const body = { work_date: date, reason: reason.trim(), items };
      const headers = { "Idempotency-Key": idempotency.current };
      return hr
        ? withStepUp(() =>
            unwrap(
              client.POST("/api/v1/employees/{employee_id}/attendance/corrections", {
                params: { path: { employee_id: employeeId }, header: headers },
                body,
              }),
            ),
          )
        : unwrap(client.POST("/api/v1/me/attendance/corrections", { params: { header: headers }, body }));
    },
    onSuccess: async (detail) => {
      await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
      toast(hr ? "Correction recorded" : "Correction sent for a decision");
      onClose();
      if (!hr) void navigate(`/attendance/corrections/${detail.correction.id}`);
    },
    onError: () => {
      // A refused attempt can be fixed and sent again with a new key.
      idempotency.current = crypto.randomUUID();
    },
  });
  const errors = fieldMessages(save.error);

  return (
    <Dialog
      title={
        hr ? "Record a correction" : workDate ? `Correct ${formatDate(workDate)}` : "Ask for a correction"
      }
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const incomplete = added.some((item) => (item.type === null) !== (item.time === ""));
          const changes = voids.length + added.filter((item) => item.type && item.time).length;
          if (incomplete) setLocal("Give each added event a type and a time, or remove the row.");
          else if (changes === 0) setLocal("Remove at least one event or add one.");
          else if (reason.trim() === "") setLocal("Say why the day needs correcting.");
          else {
            setLocal(null);
            if (!save.isPending) save.mutate();
          }
        }}
      >
        <p className="text-sm text-neutral-700">
          {hr
            ? "HR corrections apply at once. The employee and their manager are told, and the original events stay visible."
            : "Your manager or HR decides. Nothing changes until then, and the original events always stay visible."}{" "}
          Times are on the clock in {timeZone}.
        </p>
        {local ? <Banner tone="danger">{local}</Banner> : null}
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't send the correction. Try again." />
        ) : null}
        {save.isError && Object.keys(errors).length > 0 ? (
          <Banner tone="danger">{problemText(save.error, "Check the events and try again.")}</Banner>
        ) : null}
        {workDate ? null : (
          <TextField
            label="Work date"
            name="work_date"
            type="date"
            value={date}
            onChange={setDate}
            max={localIsoDate()}
          />
        )}
        {events.length > 0 ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-semibold text-neutral-800">Recorded events to remove</legend>
            {events.map((event) => (
              <Checkbox
                key={event.id}
                isSelected={voids.includes(event.id)}
                onChange={(selected) => {
                  setVoids((current) =>
                    selected ? [...current, event.id] : current.filter((id) => id !== event.id),
                  );
                }}
              >
                {eventLabel(event.event_type)} at {formatTime(event.occurred_at, timeZone)}
              </Checkbox>
            ))}
          </fieldset>
        ) : null}
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-sm font-semibold text-neutral-800">Events to add</legend>
          {added.map((item, index) => (
            <div key={item.key} className="grid items-end gap-3 sm:grid-cols-[1fr_9rem_auto_auto]">
              <Select
                label={`Event ${index + 1}`}
                name={`event-${item.key}`}
                placeholder="Choose"
                options={EVENT_OPTIONS}
                value={item.type}
                onChange={(value) => {
                  setAdded((rows) =>
                    rows.map((row) => (row.key === item.key ? { ...row, type: value as EventType } : row)),
                  );
                }}
              />
              <TextField
                label={`Time of event ${index + 1}`}
                name={`time-${item.key}`}
                type="time"
                value={item.time}
                onChange={(value) => {
                  setAdded((rows) =>
                    rows.map((row) => (row.key === item.key ? { ...row, time: value } : row)),
                  );
                }}
              />
              <div className="pb-2">
                <Checkbox
                  isSelected={item.nextDay}
                  onChange={(value) => {
                    setAdded((rows) =>
                      rows.map((row) => (row.key === item.key ? { ...row, nextDay: value } : row)),
                    );
                  }}
                >
                  Next day
                </Checkbox>
              </div>
              <Button
                variant="quiet"
                size="sm"
                aria-label={`Remove event ${index + 1}`}
                onPress={() => {
                  setAdded((rows) => rows.filter((row) => row.key !== item.key));
                }}
              >
                <Trash2 aria-hidden className="size-4" />
              </Button>
            </div>
          ))}
          <div>
            <Button
              variant="secondary"
              size="sm"
              onPress={() => {
                setAdded((rows) => [...rows, { key: Date.now(), type: null, time: "", nextDay: false }]);
              }}
            >
              <Plus aria-hidden className="size-4" />
              Add another event
            </Button>
          </div>
          {errors.items ? <p className="text-sm text-danger-700">{errors.items}</p> : null}
        </fieldset>
        <TextArea
          label="Reason"
          name="reason"
          value={reason}
          onChange={setReason}
          maxLength={1000}
          description={
            hr
              ? "Recorded with the correction and in the audit log."
              : "Shown to you and to whoever decides. It is not shown in team lists."
          }
          error={errors.reason}
        />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            {hr ? "Record correction" : "Send for decision"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
