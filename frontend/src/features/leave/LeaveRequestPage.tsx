/**
 * One leave request: its dates, how each day counted, its decisions, and what the viewer may do
 * next. The employee can withdraw a pending request or cancel leave that has not started; the
 * person who may decide it approves or rejects (a rejection needs a note). The reason shows
 * only to the employee and to whoever may decide. The API decides every action.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ConfirmDialog } from "../../design-system/Dialog";
import { DescriptionList, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDate, formatDateTime } from "../../lib/format";
import { ApiError, problemText } from "../../lib/problem";
import {
  LEAVE_KEY,
  STATUS_LABELS,
  STATUS_TONES,
  formatDays,
  formatSpan,
  type RequestDetail,
} from "./queries";

const DECISIONS: Record<RequestDetail["decisions"][number]["decision"], string> = {
  approved: "Approved",
  rejected: "Not approved",
  cancellation_approved: "Cancellation approved",
  cancellation_rejected: "Cancellation not approved",
};

export function LeaveRequestPage() {
  const { requestId = "" } = useParams();
  const request = useQuery({
    queryKey: [...LEAVE_KEY, "request", requestId],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/leave-requests/{request_id}", { params: { path: { request_id: requestId } } }),
      ),
  });
  if (request.isPending) return <Skeleton label="Loading the leave request" lines={6} />;
  if (request.isError) {
    const missing = request.error instanceof ApiError && request.error.is("not-found");
    return (
      <div>
        <BackLink />
        <PageHeader title={missing ? "Leave request not found" : "Leave request"} />
        {missing ? (
          <p className="text-sm text-neutral-700">
            This request does not exist, or it is outside the people whose leave you can see.
          </p>
        ) : (
          <ProblemMessage
            error={request.error}
            fallback="We couldn't load this leave request."
            onRetry={() => void request.refetch()}
          />
        )}
      </div>
    );
  }
  return <Detail request={request.data} />;
}

function BackLink() {
  return (
    <Link to="/leave" className="mb-4 inline-flex items-center gap-1 text-sm text-accent-700 hover:underline">
      <ArrowLeft aria-hidden className="size-4" />
      Leave
    </Link>
  );
}

function Detail({ request }: { request: RequestDetail }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [dialog, setDialog] = useState<"cancel" | "approve" | "reject" | null>(null);
  const [note, setNote] = useState("");
  const [noteError, setNoteError] = useState<string | undefined>();
  const [decisionKey, setDecisionKey] = useState(() => crypto.randomUUID());
  const cancellation = request.status === "cancellation_requested";

  const done = async (message: string) => {
    await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
    toast(message);
    setDialog(null);
    setNote("");
    setDecisionKey(crypto.randomUUID());
  };
  const cancel = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/me/leave-requests/{request_id}/cancel", {
          params: { path: { request_id: request.id } },
        }),
      ),
    onSuccess: (updated) =>
      done(updated.status === "cancelled" ? "Leave cancelled" : "Cancellation asked for"),
  });
  const decide = useMutation({
    mutationFn: (approve: boolean) => {
      const options = {
        params: { path: { request_id: request.id }, header: { "Idempotency-Key": decisionKey } },
        body: { note: note.trim() === "" ? null : note.trim() },
      };
      return unwrap(
        approve
          ? client.POST("/api/v1/leave-requests/{request_id}/approve", options)
          : client.POST("/api/v1/leave-requests/{request_id}/reject", options),
      );
    },
    onSuccess: (updated) => done(`Decision recorded: ${STATUS_LABELS[updated.status].toLowerCase()}`),
  });

  const counted = request.days.filter((day) => day.counted);
  return (
    <div className="max-w-3xl">
      <BackLink />
      <header className="mb-6">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[1.375rem] font-semibold text-neutral-900">{request.leave_type.name}</h1>
          <Badge tone={STATUS_TONES[request.status]}>{STATUS_LABELS[request.status]}</Badge>
        </div>
        <p className="mt-1 text-sm text-neutral-600">
          {request.employee.name} · {formatSpan(request)} · {formatDays(request.days_requested)}
        </p>
      </header>

      {request.can_decide || request.can_cancel ? (
        <div className="mb-6 flex flex-wrap gap-2">
          {request.can_decide ? (
            <>
              <Button
                onPress={() => {
                  setDialog("approve");
                }}
              >
                {cancellation ? "Approve the cancellation" : "Approve"}
              </Button>
              <Button
                variant="secondary"
                onPress={() => {
                  setDialog("reject");
                }}
              >
                {cancellation ? "Keep the leave" : "Reject"}
              </Button>
            </>
          ) : null}
          {request.can_cancel ? (
            <Button
              variant="secondary"
              onPress={() => {
                setDialog("cancel");
              }}
            >
              {request.status === "pending" ? "Withdraw request" : "Cancel this leave"}
            </Button>
          ) : null}
        </div>
      ) : null}
      {cancellation && request.can_decide ? (
        <div className="mb-6">
          <Banner tone="info">
            The employee asked to cancel this approved leave. Approving the cancellation returns the days to
            their balance.
          </Banner>
        </div>
      ) : null}

      <div className="flex flex-col gap-6">
        <Panel title="Request">
          <DescriptionList
            items={[
              { label: "Employee", value: request.employee.name },
              { label: "Leave type", value: request.leave_type.name },
              { label: "Dates", value: formatSpan(request) },
              { label: "Days counted", value: formatDays(request.days_requested) },
              { label: "Asked on", value: formatDateTime(request.created_at) },
              {
                label: "Reason",
                value: request.reason_visible
                  ? (request.reason ?? "None given")
                  : "Visible only to the employee and the person who decides",
              },
            ]}
          />
        </Panel>
        <Panel title="How the days counted">
          <ul className="flex flex-col divide-y divide-neutral-200 text-sm" aria-label="Days in this request">
            {request.days.map((day) => (
              <li key={day.date} className="flex justify-between gap-3 py-1.5">
                <span>{formatDate(day.date)}</span>
                <span className={day.counted ? "text-neutral-900" : "text-neutral-500"}>
                  {day.counted
                    ? Number(day.portion) < 1
                      ? "Half day"
                      : "Full day"
                    : day.not_counted_reason === "holiday"
                      ? "Holiday, not counted"
                      : "Weekly off, not counted"}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-neutral-600">
            {counted.length} of {request.days.length} days counted.
          </p>
        </Panel>
        <Panel title="Decisions">
          {request.decisions.length === 0 ? (
            <p className="text-sm text-neutral-600">No decision yet.</p>
          ) : (
            <ol className="flex flex-col gap-3 text-sm">
              {request.decisions.map((decision) => (
                <li key={decision.decided_at}>
                  <p className="font-semibold text-neutral-900">
                    {DECISIONS[decision.decision]} by {decision.decided_by}
                  </p>
                  <p className="text-neutral-600">{formatDateTime(decision.decided_at)}</p>
                  {decision.note ? <p className="mt-1 text-neutral-800">{decision.note}</p> : null}
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </div>

      <ConfirmDialog
        title={request.status === "pending" ? "Withdraw this request?" : "Cancel this leave?"}
        isOpen={dialog === "cancel"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        confirmLabel={request.status === "pending" ? "Withdraw request" : "Cancel leave"}
        isPending={cancel.isPending}
        onConfirm={() => {
          if (!cancel.isPending) cancel.mutate();
        }}
      >
        <p>
          {request.status === "pending"
            ? "The request is withdrawn and the days are no longer held back."
            : "Your manager decides the cancellation when the leave type needs approval; otherwise the days return to your balance now."}
        </p>
        {cancel.isError ? (
          <p className="mt-3 text-danger-700">{problemText(cancel.error, "We couldn't cancel it.")}</p>
        ) : null}
      </ConfirmDialog>

      <ConfirmDialog
        title={
          dialog === "approve"
            ? cancellation
              ? "Approve the cancellation?"
              : `Approve leave for ${request.employee.name}?`
            : cancellation
              ? "Keep the leave booked?"
              : `Reject the request from ${request.employee.name}?`
        }
        isOpen={dialog === "approve" || dialog === "reject"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        confirmLabel={
          dialog === "approve"
            ? cancellation
              ? "Approve cancellation"
              : "Approve"
            : cancellation
              ? "Keep the leave"
              : "Reject"
        }
        tone={dialog === "approve" ? "primary" : "danger"}
        isPending={decide.isPending}
        onConfirm={() => {
          if (dialog === "reject" && note.trim() === "") {
            setNoteError("Say why, so the employee knows what to do next.");
            return;
          }
          setNoteError(undefined);
          if (!decide.isPending) decide.mutate(dialog === "approve");
        }}
      >
        <p className="mb-3">
          {formatSpan(request)}, {formatDays(request.days_requested)} of {request.leave_type.name}.
        </p>
        <TextArea
          label={dialog === "reject" ? "Note to the employee" : "Note (optional)"}
          name="note"
          value={note}
          onChange={setNote}
          maxLength={1000}
          error={noteError}
        />
        {decide.isError ? (
          <p className="mt-3 text-danger-700">
            {problemText(decide.error, "We couldn't record the decision.")}
          </p>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
