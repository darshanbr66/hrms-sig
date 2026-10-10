/**
 * The employee's own record (self-service, CLAUDE.md rule 3: `/me` routes only). They read
 * their work details and job history, change their preferred name, and keep their personal
 * details up to date. Everything else is changed by HR.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { Badge } from "../../design-system/Badge";
import { Button } from "../../design-system/Button";
import { DataTable, type Column } from "../../design-system/DataTable";
import { Dialog } from "../../design-system/Dialog";
import { DescriptionList } from "../../design-system/Fields";
import { Panel } from "../../design-system/Panel";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { formatDate, humanize } from "../../lib/format";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { PersonalDetails } from "./PersonalDetails";
import { PEOPLE_KEY, STATUS_LABELS, STATUS_TONES, employmentTypeLabel, type Employee } from "./queries";

type Job = components["schemas"]["JobView"];

export function MyProfilePage() {
  const me = useMe();
  const profile = useQuery({
    queryKey: [...PEOPLE_KEY, "me"],
    queryFn: () => unwrap(client.GET("/api/v1/me/profile")),
  });
  const jobs = useQuery({
    queryKey: [...PEOPLE_KEY, "me", "jobs"],
    queryFn: () => unwrap(client.GET("/api/v1/me/jobs")),
  });
  const [editing, setEditing] = useState(false);

  if (profile.isPending) return <Skeleton label="Loading your profile" lines={6} />;
  if (profile.isError) {
    return (
      <ProblemMessage
        error={profile.error}
        fallback="We couldn't load your profile."
        onRetry={() => void profile.refetch()}
      />
    );
  }
  const record = profile.data;
  const job = record.job;
  const jobColumns: Column<Job>[] = [
    {
      key: "period",
      header: "From",
      primary: true,
      cell: (item) => formatDate(item.effective_from, { weekday: false }),
    },
    { key: "designation", header: "Designation", cell: (item) => item.designation.name },
    { key: "department", header: "Department", cell: (item) => item.department.name },
    { key: "location", header: "Location", cell: (item) => item.location.name },
    { key: "manager", header: "Manager", cell: (item) => item.manager?.name ?? "None" },
    { key: "reason", header: "Reason", cell: (item) => humanize(item.change_reason) },
  ];
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-center gap-4">
        <Avatar name={record.name} size="lg" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-[1.375rem] font-semibold text-neutral-900">{record.name}</h1>
            <Badge tone={STATUS_TONES[record.status]}>{STATUS_LABELS[record.status]}</Badge>
          </div>
          <p className="mt-1 text-sm text-neutral-600">
            <span className="font-mono">{record.employee_code}</span>
            {job ? ` · ${job.designation.name} · ${job.department.name}` : ""}
          </p>
        </div>
      </header>
      <Panel
        title="Work details"
        description="Kept by HR. If something is wrong, contact HR."
        action={
          hasPermission(me.data, "employee.profile.update.self") ? (
            <Button
              variant="secondary"
              size="sm"
              onPress={() => {
                setEditing(true);
              }}
            >
              <Pencil aria-hidden className="size-3.5" />
              Change preferred name
            </Button>
          ) : undefined
        }
      >
        <DescriptionList
          items={[
            {
              label: "Legal name",
              value: `${record.legal_first_name} ${record.legal_last_name ?? ""}`.trim(),
            },
            { label: "Preferred name", value: record.preferred_name },
            { label: "Work email", value: record.work_email },
            { label: "Work phone", value: record.work_phone },
            { label: "Date of joining", value: formatDate(record.date_of_joining) },
            { label: "Employment type", value: job ? employmentTypeLabel(job.employment_type) : null },
            { label: "Location", value: job?.location.name },
            { label: "Reports to", value: job?.manager?.name ?? "Nobody" },
          ]}
        />
      </Panel>
      {hasPermission(me.data, "employee.personal.read.self") ? (
        <PersonalDetails
          employeeId={null}
          canEdit={hasPermission(me.data, "employee.personal.update.self")}
        />
      ) : null}
      <Panel title="Job history" padded={false}>
        <div className="p-4">
          {jobs.isPending ? <Skeleton label="Loading your job history" lines={3} /> : null}
          {jobs.isError ? (
            <ProblemMessage
              error={jobs.error}
              fallback="We couldn't load your job history."
              onRetry={() => void jobs.refetch()}
            />
          ) : null}
          {jobs.isSuccess ? (
            <DataTable
              label="Your job history"
              columns={jobColumns}
              rows={jobs.data.items}
              rowKey={(item) => item.id}
            />
          ) : null}
        </div>
      </Panel>
      {editing ? (
        <PreferredNameDialog
          profile={record}
          onClose={() => {
            setEditing(false);
          }}
        />
      ) : null}
    </div>
  );
}

function PreferredNameDialog({ profile, onClose }: { profile: Employee; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState(profile.preferred_name ?? "");
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH("/api/v1/me/profile", {
          params: { header: { "If-Match": ifMatch(profile.version) } },
          body: { preferred_name: name.trim() === "" ? null : name.trim() },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PEOPLE_KEY });
      toast("Preferred name saved");
      onClose();
    },
  });
  const errors = fieldMessages(save.error);
  return (
    <Dialog title="Change your preferred name" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError && !errors.preferred_name ? (
          <ProblemMessage error={save.error} fallback="We couldn't save your preferred name. Try again." />
        ) : null}
        <TextField
          label="Preferred name"
          name="preferred_name"
          value={name}
          onChange={setName}
          maxLength={100}
          description="The name colleagues see in the directory. Leave it empty to use your legal first name."
          error={errors.preferred_name}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Save name
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
