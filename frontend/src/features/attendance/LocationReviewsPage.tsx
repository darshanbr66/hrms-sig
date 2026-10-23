/**
 * HR review of clock events whose reported location was flagged (ADR-031): outside every
 * approved office, inconclusive, or not checked. HR and HR admins see only people within their
 * scope and never their own events (the API decides; this page only shows its answers).
 *
 * A decision is a fixed reason plus an optional short note, and is added to the history; it never
 * changes the clock event. If the attendance itself is wrong, "Record a correction" opens the
 * usual HR correction, which has its own reason, audit and notification. No coordinates exist to
 * show: the evidence holds a rounded distance and the reported accuracy only.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MapPinned } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { DescriptionList, SearchField, TextArea } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, formatDateTime } from "../../lib/format";
import { fieldMessages, problemText } from "../../lib/problem";
import { options, useLocations } from "../organization/queries";
import { CorrectionDialog } from "./CorrectionDialog";
import { eventLabel } from "./DaysTable";
import { OUTCOME_LABELS, REASON_LABELS } from "./locationText";
import { ATTENDANCE_KEY } from "./queries";

type Evidence = components["schemas"]["LocationEvidenceView"];
type Decision = components["schemas"]["LocationReviewCreate"]["decision"];
type ReasonCode = components["schemas"]["LocationReviewCreate"]["reason_code"];

const KEY = [...ATTENDANCE_KEY, "location-reviews"] as const;
const ANY = "any";

const STATUSES = [
  { id: "open", label: "Waiting for review" },
  { id: "reviewed", label: "Reviewed" },
  { id: "all", label: "All flagged" },
];
const OUTCOMES = [
  { id: ANY, label: "Any outcome" },
  { id: "outside_radius", label: OUTCOME_LABELS.outside_radius.label },
  { id: "inconclusive", label: OUTCOME_LABELS.inconclusive.label },
  { id: "unverified", label: OUTCOME_LABELS.unverified.label },
];
const DECISIONS: { id: Decision; label: string }[] = [
  { id: "accepted", label: "Accept the clock event as it is" },
  { id: "not_accepted", label: "Do not accept it" },
];
const REASONS: { id: ReasonCode; label: string; decisions: Decision[] }[] = [
  { id: "on_site_confirmed", label: "On site, confirmed another way", decisions: ["accepted"] },
  { id: "device_or_signal_issue", label: "Device or signal problem", decisions: ["accepted"] },
  { id: "approved_offsite_work", label: "Approved work away from the office", decisions: ["accepted"] },
  {
    id: "not_on_site_correction_needed",
    label: "Not on site: attendance needs a correction",
    decisions: ["not_accepted"],
  },
  { id: "not_on_site_no_action", label: "Not on site: no change to attendance", decisions: ["not_accepted"] },
  { id: "other", label: "Other (explain in the note)", decisions: ["accepted", "not_accepted"] },
];

function reasonLabel(code: string): string {
  return REASONS.find((item) => item.id === code)?.label ?? code;
}

function OutcomeBadge({ evidence }: { evidence: Pick<Evidence, "outcome"> }) {
  const shown = OUTCOME_LABELS[evidence.outcome];
  return <Badge tone={shown.tone}>{shown.label}</Badge>;
}

function measured(evidence: Evidence): string {
  if (evidence.distance_m === null) {
    return evidence.reason_codes.map((code) => REASON_LABELS[code] ?? code).join("; ");
  }
  const distance = evidence.distance_over_cap ? "more than 10 km" : `about ${String(evidence.distance_m)} m`;
  return `${distance} from the office (radius ${String(evidence.radius_m)} m), reported accuracy ${String(evidence.accuracy_m)} m`;
}

export function LocationReviewsPage() {
  const locations = useLocations();
  const [status, setStatus] = useState("open");
  const [outcome, setOutcome] = useState(ANY);
  const [office, setOffice] = useState(ANY);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [q, setQ] = useState("");
  const [text, setText] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const list = useInfiniteQuery({
    queryKey: [...KEY, status, outcome, office, from, to, q],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/attendance/location-reviews", {
          params: {
            query: {
              status: status as "open",
              ...(outcome !== ANY ? { outcome: outcome as "outside_radius" } : {}),
              ...(office !== ANY ? { location_id: office } : {}),
              ...(from ? { from } : {}),
              ...(to ? { to } : {}),
              ...(q ? { q } : {}),
              limit: 50,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  const columns: Column<Evidence>[] = [
    {
      key: "person",
      header: "Person",
      primary: true,
      cell: (row) => (
        <span>
          {row.employee.name}
          <span className="block text-xs text-neutral-600">{row.employee.employee_code}</span>
        </span>
      ),
    },
    {
      key: "event",
      header: "Clock event",
      cell: (row) => (
        <span>
          {eventLabel(row.event_type)}
          <span className="block text-xs text-neutral-600">
            {row.event_occurred_at ? formatDateTime(row.event_occurred_at) : formatDate(row.work_date)}
            {row.event_voided ? " · removed by a correction" : ""}
          </span>
        </span>
      ),
    },
    { key: "office", header: "Office", cell: (row) => row.location_name },
    { key: "outcome", header: "Outcome", cell: (row) => <OutcomeBadge evidence={row} /> },
    {
      key: "review",
      header: "Review",
      cell: (row) =>
        row.current_review ? (
          <Badge tone={row.current_review.decision === "accepted" ? "success" : "danger"}>
            {row.current_review.decision === "accepted" ? "Accepted" : "Not accepted"}
          </Badge>
        ) : (
          <Badge tone="warning">Waiting</Badge>
        ),
    },
    {
      key: "open",
      header: "Details",
      cell: (row) => (
        <Button
          size="sm"
          variant="quiet"
          onPress={() => {
            setOpen(row.id);
          }}
        >
          Review
          <span className="sr-only">
            {" "}
            the {eventLabel(row.event_type).toLowerCase()} of {row.employee.name}
          </span>
        </Button>
      ),
    },
  ];
  return (
    <div>
      <PageHeader
        title="Location reviews"
        description="Clock events whose reported location was outside an approved office, inconclusive or not checked. A reported location is evidence, not proof: decide with what else you know. Attendance is never changed from here."
      />
      <div className="mb-4 flex flex-col gap-3">
        <SearchField
          label="Search people"
          placeholder="Search by name or code"
          value={text}
          onChange={(value) => {
            setText(value);
            if (value === "") setQ("");
          }}
          onSubmit={(value) => {
            setQ(value.trim());
          }}
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
          <Select label="Status" name="status" options={STATUSES} value={status} onChange={setStatus} />
          <Select label="Outcome" name="outcome" options={OUTCOMES} value={outcome} onChange={setOutcome} />
          <Select
            label="Office"
            name="office"
            options={[{ id: ANY, label: "All offices" }, ...options(locations.data?.items)]}
            value={office}
            onChange={setOffice}
          />
          <TextField label="From" name="from" type="date" value={from} onChange={setFrom} />
          <TextField label="To" name="to" type="date" value={to} onChange={setTo} />
        </div>
      </div>
      {list.isPending ? <Skeleton label="Loading location reviews" lines={6} /> : null}
      {list.isError ? (
        <ProblemMessage
          error={list.error}
          fallback="We couldn't load the location reviews."
          onRetry={() => void list.refetch()}
        />
      ) : null}
      {list.isSuccess && rows.length === 0 ? (
        <EmptyState
          icon={MapPinned}
          title={status === "open" ? "Nothing waiting for review" : "Nothing matches"}
        >
          Clock events appear here when their reported location needs a look.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <>
          <DataTable label="Location reviews" columns={columns} rows={rows} rowKey={(row) => row.id} />
          <LoadMore
            shown={rows.length}
            hasMore={list.hasNextPage}
            isLoading={list.isFetchingNextPage}
            onLoad={() => void list.fetchNextPage()}
          />
        </>
      ) : null}
      {open ? (
        <ReviewDialog
          evidenceId={open}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
    </div>
  );
}

function ReviewDialog({ evidenceId, onClose }: { evidenceId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const detail = useQuery({
    queryKey: [...KEY, "detail", evidenceId],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/attendance/location-reviews/{evidence_id}", {
          params: { path: { evidence_id: evidenceId } },
        }),
      ),
  });
  const [decision, setDecision] = useState<Decision | null>(null);
  const [reason, setReason] = useState<ReasonCode | null>(null);
  const [note, setNote] = useState("");
  const [correcting, setCorrecting] = useState(false);
  const data = detail.data;
  const current = data?.reviews.at(-1) ?? null;
  const decide = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/attendance/location-reviews/{evidence_id}/decisions", {
          params: { path: { evidence_id: evidenceId } },
          body: {
            decision: decision ?? "accepted",
            reason_code: reason ?? "other",
            ...(note.trim() ? { note: note.trim() } : {}),
            supersedes: current?.id ?? null,
          },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: KEY });
      toast("Review recorded");
      setDecision(null);
      setReason(null);
      setNote("");
    },
  });
  const day = useQuery({
    queryKey: [...KEY, "day", data?.evidence.employee.id, data?.evidence.work_date],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees/{employee_id}/attendance/days", {
          params: {
            path: { employee_id: data?.evidence.employee.id ?? "" },
            query: { from: data?.evidence.work_date ?? "", to: data?.evidence.work_date ?? "" },
          },
        }),
      ),
    enabled: correcting && data !== undefined,
  });
  const errors = fieldMessages(decide.error);
  const reasons = REASONS.filter((item) => decision !== null && item.decisions.includes(decision));
  return (
    <Dialog
      title={data ? `Location review: ${data.evidence.employee.name}` : "Location review"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <div className="flex flex-col gap-4">
        {detail.isPending ? <Skeleton label="Loading the review" lines={5} /> : null}
        {detail.isError ? (
          <ProblemMessage
            error={detail.error}
            fallback="We couldn't load this review."
            onRetry={() => void detail.refetch()}
          />
        ) : null}
        {data ? (
          <>
            <DescriptionList
              items={[
                {
                  label: "Person",
                  value: `${data.evidence.employee.name} (${data.evidence.employee.employee_code})`,
                },
                {
                  label: "Clock event",
                  value: `${eventLabel(data.evidence.event_type)}${data.evidence.event_occurred_at ? `, ${formatDateTime(data.evidence.event_occurred_at)}` : ""}`,
                },
                { label: "Office checked against", value: data.evidence.location_name },
                { label: "Outcome", value: <OutcomeBadge evidence={data.evidence} /> },
                { label: "What was reported", value: measured(data.evidence) },
                {
                  label: "Offices the person could clock at",
                  value: String(data.evidence.offices_considered),
                },
              ]}
            />
            {data.evidence.event_voided ? (
              <Banner tone="info">
                This clock event was removed by a correction since; it no longer counts.
              </Banner>
            ) : null}
            <section aria-label="That day's clock events">
              <h3 className="mb-2 text-sm font-semibold text-neutral-900">That day's clock events</h3>
              <ul className="flex flex-col gap-1 text-sm">
                {data.day_events.map((event) => (
                  <li
                    key={event.id}
                    className={event.voided ? "text-neutral-600 line-through" : "text-neutral-800"}
                  >
                    {formatDateTime(event.occurred_at)} {eventLabel(event.event_type)}
                    {event.id === data.evidence.event_id ? " (this one)" : ""}
                  </li>
                ))}
              </ul>
            </section>
            <section aria-label="Review history">
              <h3 className="mb-2 text-sm font-semibold text-neutral-900">Review history</h3>
              {data.reviews.length === 0 ? (
                <p className="text-sm text-neutral-600">Not reviewed yet.</p>
              ) : (
                <ol className="flex flex-col gap-2 text-sm">
                  {data.reviews.map((review) => (
                    <li key={review.id}>
                      <span className="font-semibold">
                        {review.decision === "accepted" ? "Accepted" : "Not accepted"}:{" "}
                        {reasonLabel(review.reason_code)}
                      </span>
                      <span className="block text-neutral-600">
                        {review.reviewed_by ?? "A reviewer"}, {formatDateTime(review.reviewed_at)}
                      </span>
                      {review.note ? <span className="block text-neutral-800">{review.note}</span> : null}
                    </li>
                  ))}
                </ol>
              )}
            </section>
            {data.can_review ? (
              <Form
                className="flex flex-col gap-3 border-t border-neutral-200 pt-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (decision && reason && !decide.isPending) decide.mutate();
                }}
              >
                <h3 className="text-sm font-semibold text-neutral-900">
                  {current ? "Change the decision" : "Decide"}
                </h3>
                {decide.isError && Object.keys(errors).length === 0 ? (
                  <Banner tone="danger">{problemText(decide.error, "We couldn't record the review.")}</Banner>
                ) : null}
                <div className="grid gap-3 sm:grid-cols-2">
                  <Select
                    label="Decision"
                    name="decision"
                    placeholder="Choose"
                    options={DECISIONS}
                    value={decision}
                    onChange={(value) => {
                      setDecision(value as Decision);
                      setReason(null);
                    }}
                  />
                  <Select
                    label="Reason"
                    name="reason_code"
                    placeholder="Choose"
                    options={reasons.map((item) => ({ id: item.id, label: item.label }))}
                    value={reason}
                    onChange={(value) => {
                      setReason(value as ReasonCode);
                    }}
                    isDisabled={decision === null}
                    error={errors.reason_code}
                  />
                </div>
                <TextArea
                  label="Note (optional)"
                  name="note"
                  value={note}
                  onChange={setNote}
                  maxLength={500}
                  description="Kept with the review and in the audit log. Do not include personal details that are not needed."
                  error={errors.note}
                />
                <DialogActions>
                  <Button
                    variant="secondary"
                    onPress={() => {
                      setCorrecting(true);
                    }}
                  >
                    Record a correction
                  </Button>
                  <Button type="submit" isPending={decide.isPending} isDisabled={!decision || !reason}>
                    Record review
                  </Button>
                </DialogActions>
              </Form>
            ) : (
              <DialogActions>
                <Button variant="secondary" onPress={onClose}>
                  Close
                </Button>
              </DialogActions>
            )}
          </>
        ) : null}
      </div>
      {correcting && data && day.data ? (
        <CorrectionDialog
          day={day.data.items[0] ?? null}
          workDate={data.evidence.work_date}
          timeZone={day.data.time_zone}
          employeeId={data.evidence.employee.id}
          onClose={() => {
            setCorrecting(false);
          }}
        />
      ) : null}
    </Dialog>
  );
}
