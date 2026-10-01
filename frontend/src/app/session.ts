import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "../lib/api";
import type { components } from "../lib/api-schema";
import { ApiError } from "../lib/problem";

export type Me = components["schemas"]["Me"];

export const meQueryKey = ["identity", "me"] as const;

/** The signed-in account, or null when there is no session. */
export async function fetchMe(): Promise<Me | null> {
  try {
    return await unwrap(client.GET("/api/v1/me"));
  } catch (error) {
    if (error instanceof ApiError && error.is("unauthenticated")) {
      return null;
    }
    throw error;
  }
}

export function useMe() {
  return useQuery({ queryKey: meQueryKey, queryFn: fetchMe, retry: false, staleTime: 30_000 });
}

/** Cosmetic only: the API decides every request (CLAUDE.md rule 1). */
export function hasPermission(me: Me | null | undefined, permission: string): boolean {
  return me?.permissions.includes(permission) ?? false;
}
