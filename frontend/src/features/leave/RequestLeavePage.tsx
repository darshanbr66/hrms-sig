/**
 * Ask for leave (product-requirements LV-3). The API counts the days: as the dates change, it
 * previews how each day counts (holidays the leave type does not count are left out) and what
 * the balance would be. Nothing is computed here. The form keeps one request key for its whole
 * life, so a retried submission never makes two requests.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useState } from "react";
import { Form } from "react-aria-components";
import { Link, useNavigate } from "react-router";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { EmptyState } from "../../design-system/EmptyState";
import { TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { formatDate } from "../../lib/format";
import { ApiError, fieldMessages, problemText } from "../../lib/problem";
import { LEAVE_KEY, formatDays, useBalances } from "./queries";

const HALF_OPTIONS = [
  { id: "none", label: "Full day" },
  { id: "first", label: "Morning only (first half)" },
  { id: "second", label: "Afternoon only (second half)" },
];

type Half = "first" | "second";

export function RequestLeavePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const balances = useBalances(null);
  const [requestKey] = useState(() => crypto.randomUUID());
  const [leaveType, setLeaveType] = useState<string | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [startHalf, setStartHalf] = useState("none");
  const [endHalf, setEndHalf] = useState("none");
  const [reason, setReason] = useState("");
  const [local, setLocal] = useState<Record<string, string>>({});

  const eligible = (balances.data?.items ?? []).filter((item) => item.eligible);
  const chosen = eligible.find((item) => item.leave_type.id === leaveType);
  const singleDay = start !== "" && start === end;
  const draft =
    leaveType && start && end
      ? {
          leave_type_id: leaveType,
          start_date: start,
          end_date: end,
          start_half: startHalf === "none" ? null : (startHalf as Half),
          end_half: singleDay || endHalf === "none" ? null : (endHalf as Half),
        }
      : null;
  const deferred = useDeferredValue(draft);
  const preview = useQuery({
    queryKey: [...LEAVE_KEY, "preview", deferred],
    queryFn: () => {
      if (deferred === null) throw new Error("A preview needs a leave type and dates.");
      return unwrap(client.POST("/api/v1/me/leave-requests/preview", { body: deferred }));
    },
    enabled: deferred !== null && deferred.end_date >= deferred.start_date,
    retry: false,
  });

  const submit = useMutation({
    mutationFn: () => {
      if (draft === null) throw new Error("A request needs a leave type and dates.");
      return unwrap(
        client.POST("/api/v1/me/leave-requests", {
          params: { header: { "Idempotency-Key": requestKey } },
          body: { ...draft, reason: reason.trim() === "" ? null : reason.trim() },
        }),
      );
    },
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
      toast(created.status === "approved" ? "Leave recorded" : "Leave requested");
      void navigate(`/leave/requests/${created.id}`);
    },
  });

  const apiErrors = { ...fieldMessages(preview.error), ...fieldMessages(submit.error) };
  const errors = { ...apiErrors, ...local };
  const conflict = submit.error instanceof ApiError && submit.error.is("conflict") ? submit.error : null;

  if (balances.isPending) return <Skeleton label="Loading your leave types" lines={4} />;
  if (balances.isError) {
    return (
      <ProblemMessage
        error={balances.error}
        fallback="We couldn't load your leave types."
        onRetry={() => void balances.refetch()}
      />
    );
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        title="Request leave"
        description="Choose the leave type and dates. We show how the days count before you send it."
      />
      {eligible.length === 0 ? (
        <EmptyState title="No leave type is available to you">
          HR has not set up a leave policy that covers you yet. Ask HR, or check{" "}
          <Link className="underline" to="/leave">
            My leave
          </Link>
          .
        </EmptyState>
      ) : (
        <Form
          className="flex flex-col gap-6"
          onSubmit={(event) => {
            event.preventDefault();
            const problems: Record<string, string> = {};
            if (!leaveType) problems.leave_type_id = "Choose a leave type.";
            if (!start) problems.start_date = "Choose the first day.";
            if (!end) problems.end_date = "Choose the last day.";
            if (start && end && end < start)
              problems.end_date = "The last day must be on or after the first day.";
            setLocal(problems);
            if (Object.keys(problems).length === 0 && !submit.isPending) submit.mutate();
          }}
        >
          {submit.error && !conflict && Object.keys(fieldMessages(submit.error)).length === 0 ? (
            <Banner tone="danger">
              {problemText(submit.error, "We couldn't send the request. Try again.")}
            </Banner>
          ) : null}
          {conflict ? <Banner tone="danger">{conflict.message}</Banner> : null}
          <Panel title="Leave">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Select
                  label="Leave type"
                  name="leave_type_id"
                  placeholder="Choose a leave type"
                  options={eligible.map((item) => ({
                    id: item.leave_type.id,
                    label: `${item.leave_type.name} (${formatDays(item.available)} available)`,
                  }))}
                  value={leaveType}
                  onChange={(value) => {
                    setLeaveType(value);
                    setStartHalf("none");
                    setEndHalf("none");
                  }}
                  error={errors.leave_type_id}
                />
              </div>
              <TextField
                label="First day"
                name="start_date"
                type="date"
                value={start}
                onChange={(value) => {
                  setStart(value);
                  if (!end || end < value) setEnd(value);
                }}
                error={errors.start_date}
              />
              <TextField
                label="Last day"
                name="end_date"
                type="date"
                value={end}
                onChange={setEnd}
                error={errors.end_date}
              />
              {chosen?.allows_half_day ? (
                <>
                  <Select
                    label={singleDay ? "Part of the day" : "First day: part of the day"}
                    name="start_half"
                    options={
                      singleDay ? HALF_OPTIONS : HALF_OPTIONS.filter((option) => option.id !== "first")
                    }
                    value={startHalf}
                    onChange={setStartHalf}
                    error={errors.start_half}
                  />
                  {singleDay ? null : (
                    <Select
                      label="Last day: part of the day"
                      name="end_half"
                      options={HALF_OPTIONS.filter((option) => option.id !== "second")}
                      value={endHalf}
                      onChange={setEndHalf}
                      error={errors.end_half}
                    />
                  )}
                </>
              ) : null}
              <div className="sm:col-span-2">
                <TextArea
                  label="Reason (optional)"
                  name="reason"
                  value={reason}
                  onChange={setReason}
                  maxLength={1000}
                  description="Seen by you and the person who decides the request. Not shown on calendars."
                  error={errors.reason}
                />
              </div>
            </div>
          </Panel>

          <Panel
            title="How the days count"
            description="Worked out by the HRMS from the leave type and your location's holidays."
          >
            <Preview state={preview} />
          </Panel>

          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onPress={() => {
                void navigate("/leave");
              }}
            >
              Cancel
            </Button>
            <Button type="submit" isPending={submit.isPending}>
              {chosen && !chosen.requires_approval ? "Record leave" : "Send request"}
            </Button>
          </div>
        </Form>
      )}
    </div>
  );
}

function Preview({
  state,
}: {
  state: {
    data?:
      | {
          days: {
            date: string;
            portion: string;
            counted: boolean;
            not_counted_reason: "holiday" | "weekly_off" | null;
          }[];
          days_requested: string;
          available_before: string;
          available_after: string;
          within_balance: boolean;
          requires_approval: boolean;
          weekly_offs_configured: boolean;
        }
      | undefined;
    isFetching: boolean;
    isError: boolean;
    error: unknown;
    fetchStatus: string;
    status: string;
  };
}) {
  if (state.status === "pending" && state.fetchStatus === "idle") {
    return (
      <p className="text-sm text-neutral-600">Choose a leave type and dates to see how the days count.</p>
    );
  }
  if (state.isError) {
    const fields = fieldMessages(state.error);
    const text = Object.values(fields)[0] ?? problemText(state.error, "We couldn't work out the days.");
    return (
      <p role="status" className="text-sm text-danger-700">
        {text}
      </p>
    );
  }
  if (!state.data) return <Skeleton label="Counting the days" lines={2} />;
  const data = state.data;
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <p className="text-sm text-neutral-900">
        <span className="font-semibold">{formatDays(data.days_requested)}</span> of leave.{" "}
        {formatDays(data.available_before)} available now, {formatDays(data.available_after)} after this
        request.
        {data.requires_approval
          ? " Your manager decides it."
          : " It is recorded at once; no approval is needed."}
      </p>
      {data.within_balance ? null : (
        <Banner tone="warning">This is more than your balance allows. Choose fewer days, or ask HR.</Banner>
      )}
      <ul className="flex flex-col divide-y divide-neutral-200 text-sm" aria-label="Days in this request">
        {data.days.map((day) => (
          <li key={day.date} className="flex items-center justify-between gap-3 py-1.5">
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
      {data.weekly_offs_configured ? null : (
        <p className="text-xs text-neutral-600">
          Weekly offs are not set up in the HRMS yet, so every day that is not a holiday counts.
        </p>
      )}
    </div>
  );
}
