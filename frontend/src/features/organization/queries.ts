/** Organization lists, shared by the pages that pick a department, designation or location. */
import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

export type Department = components["schemas"]["DepartmentView"];
export type Designation = components["schemas"]["DesignationView"];
export type Location = components["schemas"]["LocationView"];

export const ORG_KEY = ["org"] as const;

export function useDepartments(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...ORG_KEY, "departments", includeArchived],
    queryFn: () =>
      unwrap(client.GET("/api/v1/departments", { params: { query: { include_archived: includeArchived } } })),
    enabled,
  });
}

export function useDesignations(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...ORG_KEY, "designations", includeArchived],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/designations", { params: { query: { include_archived: includeArchived } } }),
      ),
    enabled,
  });
}

export function useLocations(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...ORG_KEY, "locations", includeArchived],
    queryFn: () =>
      unwrap(client.GET("/api/v1/locations", { params: { query: { include_archived: includeArchived } } })),
    enabled,
  });
}

export function options(items: readonly { id: string; name: string }[] | undefined) {
  return (items ?? []).map((item) => ({ id: item.id, label: item.name }));
}
