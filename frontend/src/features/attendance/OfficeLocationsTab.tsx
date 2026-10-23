/**
 * Office location checks and the offices people may clock at (ADR-031).
 *
 * - A check belongs to one office (a location) and says where it is and how a reported position
 *   is judged: radius, accepted accuracy, maximum position age, and whether unchecked presses go
 *   to HR review. Versions start today or later and are never edited; "Turn off" is a version
 *   too. HR admins (`attendance.policy.manage`) set them. Nothing is suggested by code: the form
 *   shows the pilot's proposed starting values as guidance only.
 * - People may clock at the office of their job, plus any office approved for them here (HR and
 *   HR admins, `attendance.location.assign.all`, within their scope). Approvals start today or
 *   later and end from a date; nothing is deleted.
 * - Coordinates are entered by hand, or taken from this browser's own position with "Use my
 *   current position" while standing in the office. There is no map.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MapPin, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { hasPermission, useMe } from "../../app/session";
import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ComboBox } from "../../design-system/ComboBox";
import { DataTable } from "../../design-system/DataTable";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox } from "../../design-system/Fields";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { addDays, formatDate, localIsoDate } from "../../lib/format";
import { locate } from "../../lib/geolocation";
import { fieldMessages, problemText } from "../../lib/problem";
import { useEmployeeSearch } from "../employees/queries";
import { options, useLocations } from "../organization/queries";
import { ATTENDANCE_KEY } from "./queries";

type Check = components["schemas"]["LocationCheckView"];
type Approval = components["schemas"]["OfficeApprovalView"];

const CHECKS_KEY = [...ATTENDANCE_KEY, "location-checks"] as const;

function inEffect(from: string, to: string | null): string {
  return `${formatDate(from, { weekday: false })} to ${to ? formatDate(to, { weekday: false }) : "now"}`;
}

export function OfficeLocationsTab() {
  const me = useMe();
  // Cosmetic: the API refuses every change while the installation switch is off.
  const canConfigure = hasPermission(me.data, "attendance.policy.manage");
  const canApprove = hasPermission(me.data, "attendance.location.assign.all");
  const checks = useQuery({
    queryKey: CHECKS_KEY,
    queryFn: () => unwrap(client.GET("/api/v1/attendance/location-checks")),
  });
  const [editing, setEditing] = useState<{ locationId: string | null; current: Check | null } | null>(null);
  const [approving, setApproving] = useState<string | null>(null);
  const byOffice = new Map<string, Check[]>();
  for (const check of checks.data?.items ?? []) {
    byOffice.set(check.location_id, [...(byOffice.get(check.location_id) ?? []), check]);
  }
  const enabled = checks.data?.checks_enabled ?? false;
  const canChange = canConfigure && enabled;
  const today = localIsoDate();
  const current = (versions: Check[]) =>
    versions.find((v) => v.effective_from <= today && (v.effective_to === null || v.effective_to > today)) ??
    null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-prose text-sm text-neutral-600">
          <p>
            When an office has a check, people who may clock there are asked for their reported location when
            they press a clock button. A mismatch is flagged for HR review; attendance is never refused.
          </p>
          <p className="mt-2">
            People may clock at the office of their job and at any office approved for them below. Turn checks
            on one office at a time.
          </p>
        </div>
        {canChange ? (
          <Button
            onPress={() => {
              setEditing({ locationId: null, current: null });
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add an office check
          </Button>
        ) : null}
      </div>
      {checks.isSuccess && !enabled ? (
        <Banner tone="info">
          Office location checks are turned off for this installation. No office check can be added and none
          applies to clock events. They are turned on only after the retention period and the privacy notice
          are approved.
        </Banner>
      ) : null}
      {checks.isPending ? <Skeleton label="Loading office checks" lines={3} /> : null}
      {checks.isError ? (
        <ProblemMessage
          error={checks.error}
          fallback="We couldn't load the office checks."
          onRetry={() => void checks.refetch()}
        />
      ) : null}
      {checks.isSuccess && byOffice.size === 0 ? (
        <EmptyState icon={MapPin} title="No office has a location check">
          Nobody is asked for a location until an office has a check that is on.
        </EmptyState>
      ) : null}
      {[...byOffice.entries()].map(([locationId, versions]) => {
        const now = current(versions);
        const name = versions[0]?.location_name ?? "Office";
        return (
          <Panel
            key={locationId}
            title={name}
            description={
              now
                ? now.mode === "verify"
                  ? `Checks are on: within ${String(now.radius_m)} m, accuracy up to ${String(now.max_accuracy_m)} m, positions up to ${String(now.max_position_age_seconds)} s old.`
                  : "Checks are off."
                : "No version in effect today."
            }
            action={
              <div className="flex flex-wrap gap-2">
                {canApprove ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onPress={() => {
                      setApproving(locationId);
                    }}
                  >
                    Approved people
                  </Button>
                ) : null}
                {canChange ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onPress={() => {
                      setEditing({ locationId, current: now ?? versions[0] ?? null });
                    }}
                  >
                    New version<span className="sr-only"> of the check for {name}</span>
                  </Button>
                ) : null}
              </div>
            }
          >
            <DataTable
              label={`Check versions for ${name}`}
              rows={versions}
              rowKey={(row) => row.id}
              columns={[
                {
                  key: "dates",
                  header: "In effect",
                  primary: true,
                  cell: (row) => inEffect(row.effective_from, row.effective_to),
                },
                {
                  key: "mode",
                  header: "Checks",
                  cell: (row) => (
                    <Badge tone={row.mode === "verify" ? "success" : "neutral"}>
                      {row.mode === "verify" ? "On" : "Off"}
                    </Badge>
                  ),
                },
                {
                  key: "position",
                  header: "Office position",
                  cell: (row) => `${row.latitude.toFixed(6)}, ${row.longitude.toFixed(6)}`,
                },
                { key: "radius", header: "Radius", align: "end", cell: (row) => `${String(row.radius_m)} m` },
                {
                  key: "accuracy",
                  header: "Accepted accuracy",
                  align: "end",
                  cell: (row) => `${String(row.max_accuracy_m)} m`,
                },
                {
                  key: "review",
                  header: "Unchecked presses",
                  cell: (row) => (row.review_unverified ? "Sent to review" : "Not reviewed"),
                },
              ]}
            />
          </Panel>
        );
      })}
      {editing ? (
        <CheckDialog
          locationId={editing.locationId}
          current={editing.current}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
      {approving ? (
        <ApprovalsDialog
          locationId={approving}
          name={byOffice.get(approving)?.[0]?.location_name ?? "this office"}
          onClose={() => {
            setApproving(null);
          }}
        />
      ) : null}
    </div>
  );
}

function CheckDialog({
  locationId,
  current,
  onClose,
}: {
  locationId: string | null;
  current: Check | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const locations = useLocations();
  const [location, setLocation] = useState<string | null>(locationId);
  const [from, setFrom] = useState(localIsoDate());
  const [on, setOn] = useState(current ? current.mode === "verify" : true);
  const [latitude, setLatitude] = useState(current ? String(current.latitude) : "");
  const [longitude, setLongitude] = useState(current ? String(current.longitude) : "");
  const [radius, setRadius] = useState(current ? String(current.radius_m) : "");
  const [accuracy, setAccuracy] = useState(current ? String(current.max_accuracy_m) : "");
  const [age, setAge] = useState(current ? String(current.max_position_age_seconds) : "");
  const [review, setReview] = useState(current ? current.review_unverified : true);
  const [here, setHere] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/attendance/location-checks", {
          body: {
            location_id: location ?? "",
            effective_from: from,
            mode: on ? "verify" : "off",
            latitude: Number(latitude),
            longitude: Number(longitude),
            radius_m: Number(radius),
            max_accuracy_m: Number(accuracy),
            max_position_age_seconds: Number(age),
            review_unverified: review,
          },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: CHECKS_KEY });
      toast("Office check saved");
      onClose();
    },
  });
  const fillFromHere = async () => {
    setHere("Getting this browser's position…");
    const report = await locate();
    if (report.status !== "captured") {
      setHere("This browser could not give its position. Enter the coordinates instead.");
      return;
    }
    setLatitude(report.latitude.toFixed(6));
    setLongitude(report.longitude.toFixed(6));
    setHere(
      `Filled from this browser, accurate to about ${String(Math.round(report.accuracy_m))} m. Check them before saving.`,
    );
  };
  const errors = fieldMessages(save.error);
  return (
    <Dialog
      title={current ? "New version of the office check" : "Add an office check"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <p className="text-sm text-neutral-700">
          The new version starts on the chosen date, today or later, and ends the current one. Versions that
          already started are never changed, so past clock events keep the settings they were checked with.
        </p>
        {save.isError && Object.keys(errors).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't save the office check." />
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Select
            label="Office"
            name="location_id"
            placeholder="Choose"
            options={options(locations.data?.items)}
            value={location}
            onChange={setLocation}
            isDisabled={locationId !== null}
            error={errors.location_id}
          />
          <TextField
            label="From"
            name="effective_from"
            type="date"
            value={from}
            onChange={setFrom}
            min={localIsoDate()}
            error={errors.effective_from}
          />
        </div>
        <Checkbox isSelected={on} onChange={setOn}>
          Checks are on for this office from that date
        </Checkbox>
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-sm font-semibold text-neutral-800">Where the office is</legend>
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField
              label="Latitude"
              name="latitude"
              value={latitude}
              onChange={setLatitude}
              inputMode="decimal"
              error={errors.latitude}
            />
            <TextField
              label="Longitude"
              name="longitude"
              value={longitude}
              onChange={setLongitude}
              inputMode="decimal"
              error={errors.longitude}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="secondary" onPress={() => void fillFromHere()}>
              <MapPin aria-hidden className="size-4" />
              Use my current position
            </Button>
            {latitude && longitude ? (
              <a
                className="text-sm text-accent-700 underline"
                href={`https://www.openstreetmap.org/?mlat=${encodeURIComponent(latitude)}&mlon=${encodeURIComponent(longitude)}#map=18/${encodeURIComponent(latitude)}/${encodeURIComponent(longitude)}`}
                target="_blank"
                rel="noreferrer noopener"
              >
                Check on OpenStreetMap
              </a>
            ) : null}
          </div>
          {here ? <p className="text-sm text-neutral-700">{here}</p> : null}
        </fieldset>
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-sm font-semibold text-neutral-800">
            How a reported position is judged
          </legend>
          <p className="text-sm text-neutral-600">
            Proposed starting values for the pilot: a 150 m radius and 100 m accepted accuracy. They are not
            right for every office: phones outdoors are often accurate to tens of metres, laptops often to
            hundreds or worse.
          </p>
          <div className="grid gap-4 sm:grid-cols-3">
            <TextField
              label="Radius (m)"
              name="radius_m"
              value={radius}
              onChange={setRadius}
              inputMode="numeric"
              description="10 to 5000"
              error={errors.radius_m}
            />
            <TextField
              label="Accepted accuracy (m)"
              name="max_accuracy_m"
              value={accuracy}
              onChange={setAccuracy}
              inputMode="numeric"
              description="1 to 5000"
              error={errors.max_accuracy_m}
            />
            <TextField
              label="Maximum position age (s)"
              name="max_position_age_seconds"
              value={age}
              onChange={setAge}
              inputMode="numeric"
              description="10 to 600; the pilot uses 120"
              error={errors.max_position_age_seconds}
            />
          </div>
          <Checkbox isSelected={review} onChange={setReview}>
            Send presses whose location could not be checked to HR review
          </Checkbox>
        </fieldset>
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            isPending={save.isPending}
            isDisabled={!location || !latitude || !longitude || !radius || !accuracy || !age}
          >
            Save version
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function ApprovalsDialog({
  locationId,
  name,
  onClose,
}: {
  locationId: string;
  name: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const search = useEmployeeSearch(null);
  const key = [...ATTENDANCE_KEY, "office-approvals", locationId];
  const approvals = useQuery({
    queryKey: key,
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/attendance/office-approvals", { params: { query: { location_id: locationId } } }),
      ),
  });
  const [pick, setPick] = useState<string | null>(null);
  const [from, setFrom] = useState(localIsoDate());
  const refresh = async (message: string) => {
    await queryClient.invalidateQueries({ queryKey: key });
    toast(message);
  };
  const approve = useMutation({
    mutationFn: (employeeId: string) =>
      unwrap(
        client.POST("/api/v1/attendance/office-approvals", {
          body: { employee_id: employeeId, location_id: locationId, effective_from: from },
        }),
      ),
    onSuccess: () => refresh("Office approved"),
  });
  const end = useMutation({
    mutationFn: (approval: Approval) =>
      unwrap(
        client.POST("/api/v1/attendance/office-approvals/{approval_id}/end", {
          params: { path: { approval_id: approval.id } },
          // The later of today and the day after the approval starts (an end is after the start).
          body: {
            effective_to:
              [localIsoDate(), addDays(approval.effective_from, 1)].sort().at(-1) ?? localIsoDate(),
          },
        }),
      ),
    onSuccess: () => refresh("Approval ended"),
  });
  const rows = approvals.data?.items ?? [];
  const failure = approve.error ?? end.error;
  return (
    <Dialog title={`People approved for ${name}`} isOpen onOpenChange={onClose} size="lg">
      <div className="flex flex-col gap-4">
        <p className="text-sm text-neutral-700">
          Everyone whose job is at {name} may clock there already. Approve other people who also work here; an
          approval starts today or later and ends from the day you choose. Nothing is deleted.
        </p>
        {failure ? (
          <Banner tone="danger">{problemText(failure, "We couldn't change the approval.")}</Banner>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-[1fr_12rem] sm:items-end">
          <ComboBox
            label="Approve a person"
            options={search.options}
            selectedKey={pick}
            onSelectionChange={(key) => {
              setPick(null);
              const option = search.options.find((item) => item.id === key);
              if (option) approve.mutate(option.id);
              search.setText("");
            }}
            inputValue={search.text}
            onInputChange={search.setText}
            emptyText="No employee you can see matches."
          />
          <TextField
            label="From"
            name="approval_from"
            type="date"
            value={from}
            onChange={setFrom}
            min={localIsoDate()}
          />
        </div>
        {approvals.isPending ? <Skeleton label="Loading approvals" lines={2} /> : null}
        {approvals.isSuccess && rows.length === 0 ? (
          <p className="text-sm text-neutral-600">Nobody else is approved for this office.</p>
        ) : null}
        {rows.length > 0 ? (
          <DataTable
            label={`Approvals for ${name}`}
            rows={rows}
            rowKey={(row) => row.id}
            columns={[
              {
                key: "person",
                header: "Person",
                primary: true,
                cell: (row) => (
                  <span>
                    {row.employee_name}
                    <span className="block text-xs text-neutral-600">{row.employee_code}</span>
                  </span>
                ),
              },
              {
                key: "dates",
                header: "Approved",
                cell: (row) => inEffect(row.effective_from, row.effective_to),
              },
              {
                key: "actions",
                header: "Actions",
                align: "end",
                cell: (row) =>
                  row.effective_to === null ? (
                    <Button
                      size="sm"
                      variant="quiet"
                      isPending={end.isPending && end.variables.id === row.id}
                      onPress={() => {
                        end.mutate(row);
                      }}
                    >
                      End<span className="sr-only"> the approval of {row.employee_name}</span>
                    </Button>
                  ) : null,
              },
            ]}
          />
        ) : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Close
          </Button>
        </DialogActions>
      </div>
    </Dialog>
  );
}
