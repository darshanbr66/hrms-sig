/**
 * A stand-in for the API in component tests. Handlers are keyed by "METHOD /path"; every call
 * is recorded, so a test can assert what the UI sent as well as what it showed.
 */
import { vi } from "vitest";

import type { Me } from "../app/session";

export interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
}

type Handler = (call: Call) => Response | Promise<Response>;

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json", ...headers },
  });
}

export function problem(
  type: string,
  status: number,
  extra: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): Response {
  return json(
    {
      type: `/problems/${type}`,
      title: TITLES[type] ?? "Problem",
      status,
      detail: null,
      request_id: "req-1",
      ...extra,
    },
    status,
    headers,
  );
}

const TITLES: Record<string, string> = {
  unauthenticated: "Sign in to continue.",
  "invalid-credentials":
    "The details you entered are not correct. Check them and try again. If you keep having trouble, contact HR.",
  "step-up-required": "Enter a code from your authenticator app to continue.",
  "rate-limited": "Too many requests. Wait a moment, then try again.",
  "temporarily-unavailable": "This service is temporarily unavailable. Try again shortly.",
  forbidden: "You don't have access to this.",
};

export class FakeApi {
  readonly calls: Call[] = [];
  private readonly handlers = new Map<string, Handler[]>();

  /** Answer the next request to `route`; the last handler for a route keeps answering. */
  on(route: string, handler: Handler | Response): this {
    const queue = this.handlers.get(route) ?? [];
    queue.push(handler instanceof Response ? () => handler.clone() : handler);
    this.handlers.set(route, queue);
    return this;
  }

  install(): void {
    vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
      const request =
        input instanceof Request ? input : new Request(new URL(input, window.location.origin), init);
      const url = new URL(request.url);
      const text = await request.text();
      const call: Call = {
        method: request.method,
        path: url.pathname,
        body: text ? (JSON.parse(text) as unknown) : null,
        headers: request.headers,
      };
      this.calls.push(call);
      const queue = this.handlers.get(`${call.method} ${call.path}`);
      if (!queue || queue.length === 0) {
        return problem("not-found", 404);
      }
      const handler = queue.length > 1 ? queue.shift() : queue[0];
      return handler ? handler(call) : problem("not-found", 404);
    });
  }

  called(route: string): Call[] {
    return this.calls.filter((call) => `${call.method} ${call.path}` === route);
  }
}

export const ME: Me = {
  user_id: "01900000-0000-7000-8000-000000000001",
  email: "person@dev.example",
  employee_id: null,
  session_id: "01900000-0000-7000-8000-000000000002",
  session_scope: "full",
  step_up_expires_at: null,
  roles: [],
  permissions: ["auth.session.read.self", "auth.session.revoke.self"],
};
