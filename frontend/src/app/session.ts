import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "../lib/api";
import type { components } from "../lib/api-schema";
import { ApiError } from "../lib/problem";

export type Me = components["schemas"]["Me"];

export const meQueryKey = ["identity", "me"] as const;
export const ELEVATION_KEY = ["access", "elevation"] as const;

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

/** `enabled: false` waits (the sign-in page asks only once the API answers its readiness
 * check); `fresh` asks the API again on mount instead of relying on a cached answer. */
export function useMe({ enabled = true, fresh = false }: { enabled?: boolean; fresh?: boolean } = {}) {
  return useQuery({
    queryKey: meQueryKey,
    queryFn: fetchMe,
    retry: false,
    staleTime: 30_000,
    enabled,
    refetchOnMount: fresh ? "always" : true,
  });
}

/** Cosmetic only: the API decides every request (CLAUDE.md rule 1). */
export function hasPermission(me: Me | null | undefined, permission: string): boolean {
  return me?.permissions.includes(permission) ?? false;
}
