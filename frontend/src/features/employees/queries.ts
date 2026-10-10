/** Employee record queries and labels shared by the people pages. */
import { useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState } from "react";

import type { BadgeTone } from "../../design-system/Badge";
import type { ComboOption } from "../../design-system/ComboBox";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

export type Employee = components["schemas"]["EmployeeView"];
export type EmployeeSummary = components["schemas"]["EmployeeSummary"];
export type EmployeeStatus = Employee["status"];
export type EmploymentType = components["schemas"]["JobAssignment"]["employment_type"];

export const PEOPLE_KEY = ["people"] as const;

export const STATUS_LABELS: Record<EmployeeStatus, string> = {
  pre_joining: "Joining soon",
  active: "Active",
  on_notice: "On notice",
  exited: "Left",
};

export const STATUS_TONES: Record<EmployeeStatus, BadgeTone> = {
  pre_joining: "info",
  active: "success",
  on_notice: "warning",
  exited: "neutral",
};

export const EMPLOYMENT_TYPES: { id: EmploymentType; label: string }[] = [
  { id: "full_time", label: "Full time" },
  { id: "part_time", label: "Part time" },
  { id: "contract", label: "Contract" },
  { id: "intern", label: "Intern" },
];

export function employmentTypeLabel(value: EmploymentType): string {
  return EMPLOYMENT_TYPES.find((item) => item.id === value)?.label ?? value;
}

export function useEmployee(employeeId: string) {
  return useQuery({
    queryKey: [...PEOPLE_KEY, "employee", employeeId],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees/{employee_id}", { params: { path: { employee_id: employeeId } } }),
      ),
  });
}

export function useStatistics(enabled: boolean) {
  return useQuery({
    queryKey: [...PEOPLE_KEY, "statistics"],
    queryFn: () => unwrap(client.GET("/api/v1/employees/statistics")),
    enabled,
  });
}

/** Search people the viewer may read, for choosing a manager or a department head. */
export function useEmployeeSearch(initial: { id: string; name: string } | null, excludeId?: string) {
  const [text, setText] = useState(initial?.name ?? "");
  const deferred = useDeferredValue(text);
  const query = useQuery({
    queryKey: [...PEOPLE_KEY, "search", deferred],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/employees", {
          params: {
            query: {
              ...(deferred.trim().length > 0 ? { q: deferred.trim() } : {}),
              status: ["active", "on_notice", "pre_joining"],
              limit: 20,
            },
          },
        }),
      ),
  });
  const options: ComboOption[] = (query.data?.items ?? [])
    .filter((item) => item.id !== excludeId)
    .map((item) => ({
      id: item.id,
      label: item.name,
      detail: [item.employee_code, item.job?.designation.name].filter(Boolean).join(" · "),
    }));
  if (initial && !options.some((option) => option.id === initial.id)) {
    options.unshift({ id: initial.id, label: initial.name });
  }
  return { text, setText, options };
}
