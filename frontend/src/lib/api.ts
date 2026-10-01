/**
 * The one API client (docs/api-architecture.md §12).
 *
 * - Sends `X-Requested-With: sv-web` on every request (the API's CSRF check) and relies on the
 *   HttpOnly session cookies; the SPA never sees or stores a token.
 * - On a 401 it refreshes the session once and retries. Refreshes are single-flight: one at a
 *   time in this tab, and one at a time across tabs through the Web Locks API, because a
 *   refresh token works once and presenting it twice revokes the session.
 * - If the refresh fails, the session has ended: listeners are told, and the app returns to
 *   sign-in.
 */
import createClient from "openapi-fetch";

import type { paths } from "./api-schema";
import { ApiError, networkProblem, toApiError } from "./problem";

export const CSRF_HEADER = { "X-Requested-With": "sv-web" } as const;
const REFRESH_PATH = "/api/v1/auth/refresh";
// Requests whose 401 means "wrong credentials", not "session expired".
const NO_REFRESH = ["/api/v1/auth/login", "/api/v1/auth/login/mfa", REFRESH_PATH, "/api/v1/auth/invite/"];

type SessionListener = () => void;
const sessionEndedListeners = new Set<SessionListener>();

export function onSessionEnded(listener: SessionListener): () => void {
  sessionEndedListeners.add(listener);
  return () => sessionEndedListeners.delete(listener);
}

let refreshing: Promise<boolean> | null = null;

async function postRefresh(): Promise<boolean> {
  try {
    const response = await fetch(REFRESH_PATH, {
      method: "POST",
      headers: CSRF_HEADER,
      credentials: "same-origin",
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Refresh the session once, however many requests or tabs ask at the same time. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (
    typeof navigator !== "undefined" && "locks" in navigator
      ? navigator.locks.request("sv-session-refresh", postRefresh)
      : postRefresh()
  ).finally(() => {
    refreshing = null;
  });
  return refreshing;
}

async function fetchWithRefresh(input: Request): Promise<Response> {
  const retry = input.clone();
  const response = await fetch(input);
  const path = new URL(input.url, window.location.origin).pathname;
  if (response.status !== 401 || NO_REFRESH.some((prefix) => path.startsWith(prefix))) {
    return response;
  }
  if (await refreshSession()) {
    return fetch(retry);
  }
  sessionEndedListeners.forEach((listener) => {
    listener();
  });
  return response;
}

export const client = createClient<paths>({
  baseUrl: window.location.origin,
  headers: CSRF_HEADER,
  credentials: "same-origin",
  fetch: fetchWithRefresh,
});

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** The response body, or an ApiError carrying the problem details. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await call;
  } catch {
    throw new ApiError(networkProblem());
  }
  if (result.error !== undefined || !result.response.ok) {
    throw toApiError(result.error, result.response);
  }
  return result.data as T;
}
