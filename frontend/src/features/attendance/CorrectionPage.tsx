/** One attendance correction: the day before and after it side by side (ATT-7), the reason for
 * those allowed to see it, and the decision or withdrawal when the viewer may make it. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Link, useParams } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ConfirmDialog, Dialog, DialogActions } from "../../design-system/Dialog";
import { DescriptionList, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDate, formatDateTime, formatTime } from "../../lib/format";
import { ApiError, fieldMessages } from "../../lib/problem";
import { eventLabel } from "./DaysTable";
import {
  ATTENDANCE_KEY,
  CORRECTION_STATUS_LABELS,
  CORRECTION_STATUS_TONES,
  type CorrectionDetail,
} from "./queries";

type TimelineEvent = CorrectionDetail["before"][number];

function Timeline({ title, events, zone }: { title: string; events: TimelineEvent[]; zone: string }) {
  return (
    <Panel title={title}>
      {events.length === 0 ? (
        <p className="text-sm text-neutral-600">No events.</p>
      ) : (
        <ol aria-label={title} className="flex flex-col gap-2">
          {events.map((event) => (
            <li
              key={`${event.event_type}-${event.occurred_at}`}
              className="flex justify-between gap-4 text-sm"
            >
              <span className="text-neutral-800">{eventLabel(event.event_type)}</span>
              <span className="tabular font-semibold text-neutral-900">
                {formatTime(event.occurred_at, zone)}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

export function CorrectionPage() {
  const { correctionId = "" } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [rejecting, setRejecting] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  const [note, setNote] = useState("");
  const detail = useQuery({
    queryKey: [...ATTENDANCE_KEY, "correction", correctionId],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/attendance/corrections/{correction_id}", {
          params: { path: { correction_id: correctionId } },
        }),
      ),
  });
  const done = async (message: string) => {
    await queryClient.invalidateQueries({ queryKey: ATTENDANCE_KEY });
    toast(message);
  };
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/attendance/corrections/{correction_id}/approve", {
          params: { path: { correction_id: correctionId } },
          body: { note: note.trim() === "" ? null : note.trim() },
        }),
      ),
    onSuccess: () => done("Correction approved"),
  });
  const reject = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/attendance/corrections/{correction_id}/reject", {
          params: { path: { correction_id: correctionId } },
          body: { note: note.trim() },
        }),
      ),
    onSuccess: async () => {
      setRejecting(false);
      await done("Correction not approved");
    },
  });
  const withdraw = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/me/attendance/corrections/{correction_id}/cancel", {
          params: { path: { correction_id: correctionId } },
        }),
      ),
    onSuccess: async () => {
      setWithdrawing(false);
      await done("Correction withdrawn");
    },
  });

  if (detail.isPending) return <Skeleton label="Loading the correction" lines={6} />;
  if (detail.isError) {
    const missing = detail.error instanceof ApiError && detail.error.is("not-found");
    return (
      <div>
        <PageHeader title={missing ? "Correction not found" : "Attendance correction"} />
        {missing ? (
          <p className="text-sm text-neutral-700">
            This correction does not exist, or it is outside what you can see.
          </p>
        ) : (
          <ProblemMessage
            error={detail.error}
            fallback="We couldn't load the correction."
            onRetry={() => void detail.refetch()}
          />
        )}
      </div>
    );
  }
  const data = detail.data;
  const c = data.correction;
  const decided = c.status === "approved" || c.status === "rejected";
  return (
    <div className="flex flex-col gap-6">
      <Link
        to="/attendance"
        className="inline-flex items-center gap-1 text-sm text-accent-700 hover:underline"
      >
        <ArrowLeft aria-hidden className="size-4" />
        Attendance
      </Link>
      <PageHeader
        title={`Correction for ${formatDate(c.work_date)}`}
        description={`${c.employee_name} · times on the clock in ${data.time_zone}`}
        action={
          <span className="flex flex-wrap gap-2">
            <Badge tone={CORRECTION_STATUS_TONES[c.status]}>{CORRECTION_STATUS_LABELS[c.status]}</Badge>
            {c.locked_period ? <Badge tone="neutral">Locked period</Badge> : null}
          </span>
        }
      />
      {c.status === "pending" && !data.still_valid ? (
        <Banner tone="warning">
          The day changed after this was asked, so it no longer fits as it is. It can be withdrawn and asked
          again.
        </Banner>
      ) : null}
      <div className="grid gap-4 md:grid-cols-2">
        <Timeline
          title={c.status === "approved" ? "Before the correction" : "As recorded now"}
          events={data.before}
          zone={data.time_zone}
        />
        <Timeline
          title={c.status === "approved" ? "After the correction" : "With the correction"}
          events={data.after}
          zone={data.time_zone}
        />
      </div>
      <Panel title="Details">
        <DescriptionList
          items={[
            { label: c.kind === "hr_recorded" ? "Recorded by" : "Asked by", value: data.requested_by },
            { label: "Asked on", value: formatDateTime(c.requested_at) },
            { label: "Reason", value: data.reason ?? "Shown only to the employee and whoever decides" },
            ...(decided
              ? [
                  { label: "Decided by", value: data.decided_by },
                  { label: "Decided on", value: c.decided_at ? formatDateTime(c.decided_at) : null },
                  ...(data.decision_note ? [{ label: "Note", value: data.decision_note }] : []),
                ]
              : []),
          ]}
        />
      </Panel>
      {approve.isError ? (
        <ProblemMessage error={approve.error} fallback="We couldn't approve the correction." />
      ) : null}
      {withdraw.isError ? (
        <ProblemMessage error={withdraw.error} fallback="We couldn't withdraw the correction." />
      ) : null}
      {data.can_decide ? (
        <div className="flex flex-wrap gap-2">
          <Button
            onPress={() => {
              approve.mutate();
            }}
            isPending={approve.isPending}
          >
            Approve correction
          </Button>
          <Button
            variant="secondary"
            onPress={() => {
              setRejecting(true);
            }}
          >
            Do not approve
          </Button>
        </div>
      ) : null}
      {data.can_cancel ? (
        <div>
          <Button
            variant="secondary"
            onPress={() => {
              setWithdrawing(true);
            }}
          >
            Withdraw correction
          </Button>
        </div>
      ) : null}
      <Dialog title="Do not approve this correction" isOpen={rejecting} onOpenChange={setRejecting}>
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!reject.isPending) reject.mutate();
          }}
        >
          {reject.isError && Object.keys(fieldMessages(reject.error)).length === 0 ? (
            <ProblemMessage error={reject.error} fallback="We couldn't record the decision." />
          ) : null}
          <TextArea
            label="Note to the employee"
            name="note"
            value={note}
            onChange={setNote}
            maxLength={1000}
            description="Say why, so they know what to do next."
            error={fieldMessages(reject.error).note}
          />
          <DialogActions>
            <Button
              variant="secondary"
              onPress={() => {
                setRejecting(false);
              }}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="danger"
              isPending={reject.isPending}
              isDisabled={note.trim() === ""}
            >
              Do not approve
            </Button>
          </DialogActions>
        </Form>
      </Dialog>
      <ConfirmDialog
        title="Withdraw this correction?"
        isOpen={withdrawing}
        onOpenChange={setWithdrawing}
        confirmLabel="Withdraw correction"
        onConfirm={() => {
          withdraw.mutate();
        }}
        isPending={withdraw.isPending}
      >
        Nothing about the day changes. You can ask for a new correction afterwards.
      </ConfirmDialog>
    </div>
  );
}
