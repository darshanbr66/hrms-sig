/** Every leave request in the viewer's scope, with filters kept in the URL: status, leave type,
 * department, location and dates. Reasons stay on the request page. */
import { useSearchParams } from "react-router";

import { hasPermission, useMe } from "../../app/session";
import { PageHeader } from "../../design-system/PageHeader";
import { Select } from "../../design-system/Select";
import { TextField } from "../../design-system/TextField";
import { options, useDepartments, useLocations } from "../organization/queries";
import { STATUS_LABELS, useLeaveTypes, type RequestStatus } from "./queries";
import { RequestTable, type RequestQuery } from "./RequestTable";

const ANY = "any";

export function LeaveOverviewPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const canReadOrg = hasPermission(me.data, "org.read");
  const types = useLeaveTypes(true);
  const departments = useDepartments(false, canReadOrg);
  const locations = useLocations(false, canReadOrg);
  const value = (key: string) => params.get(key) ?? ANY;
  const update = (key: string, next: string) => {
    const copy = new URLSearchParams(params);
    if (next === "" || next === ANY) copy.delete(key);
    else copy.set(key, next);
    setParams(copy, { replace: true });
  };
  const query: Omit<RequestQuery, "cursor" | "limit"> = {
    ...(value("status") !== ANY ? { status: [value("status") as RequestStatus] } : {}),
    ...(value("type") !== ANY ? { leave_type_id: value("type") } : {}),
    ...(value("department") !== ANY ? { department_id: value("department") } : {}),
    ...(value("location") !== ANY ? { location_id: value("location") } : {}),
    ...(params.get("from") ? { from: params.get("from") ?? "" } : {}),
    ...(params.get("to") ? { to: params.get("to") ?? "" } : {}),
  };
  return (
    <div>
      <PageHeader
        title="Leave overview"
        description="Leave requests of the people your role covers, the latest leave first."
      />
      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 xl:items-end">
        <Select
          label="Status"
          name="status"
          options={[
            { id: ANY, label: "Any status" },
            ...Object.entries(STATUS_LABELS).map(([id, label]) => ({ id, label })),
          ]}
          value={value("status")}
          onChange={(next) => {
            update("status", next);
          }}
        />
        <Select
          label="Leave type"
          name="type"
          options={[
            { id: ANY, label: "All leave types" },
            ...(types.data?.items ?? []).map((t) => ({ id: t.id, label: t.name })),
          ]}
          value={value("type")}
          onChange={(next) => {
            update("type", next);
          }}
        />
        {canReadOrg ? (
          <>
            <Select
              label="Department"
              name="department"
              options={[{ id: ANY, label: "All departments" }, ...options(departments.data?.items)]}
              value={value("department")}
              onChange={(next) => {
                update("department", next);
              }}
            />
            <Select
              label="Location"
              name="location"
              options={[{ id: ANY, label: "All locations" }, ...options(locations.data?.items)]}
              value={value("location")}
              onChange={(next) => {
                update("location", next);
              }}
            />
          </>
        ) : null}
        <TextField
          label="From"
          name="from"
          type="date"
          value={params.get("from") ?? ""}
          onChange={(next) => {
            update("from", next);
          }}
        />
        <TextField
          label="To"
          name="to"
          type="date"
          value={params.get("to") ?? ""}
          onChange={(next) => {
            update("to", next);
          }}
        />
      </div>
      <RequestTable
        label="Leave requests"
        query={query}
        showEmployee
        empty={{ title: "No leave requests match", body: "Change the filters to see more requests." }}
      />
    </div>
  );
}
