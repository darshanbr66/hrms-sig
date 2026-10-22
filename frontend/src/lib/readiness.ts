/**
 * Whether the API is up, from its public readiness check (`GET /api/health/ready`, sent through
 * the one API client like every other call).
 *
 * The production API sleeps when nobody has used it for a while and takes up to about a minute
 * to start again. The sign-in page starts a check when it opens, and sends the sign-in request
 * only after a check has passed, so a password is never sent to an API that is still starting.
 *
 * - Bounded: at most `maxAttempts` requests, each cut off after `attemptTimeoutMs`, with growing
 *   pauses between them, all within `totalTimeoutMs`. After that the check stops and waits for
 *   the person to retry; it never polls on its own.
 * - Single-flight: callers that ask while a check runs share it.
 * - "ready" only after the check has answered `{"status": "ok"}`, and only for `freshForMs`: an
 *   older answer is checked again (one request when the API is awake) before it is relied on.
 * - Only the readiness check is repeated automatically. It is a GET that changes nothing.
 */
import { useEffect, useSyncExternalStore } from "react";

import { client } from "./api";

export const HEALTH_PATH = "/api/health/ready";

export type ReadinessPhase = "idle" | "connecting" | "waking" | "ready" | "unavailable";

export interface ReadinessOptions {
  /** One request is abandoned after this long. */
  attemptTimeoutMs: number;
  /** The whole check gives up after this long. */
  totalTimeoutMs: number;
  /** Pauses between attempts; the last one repeats. */
  delaysMs: readonly number[];
  maxAttempts: number;
  /** A first attempt still running after this long shows the starting-up message. */
  slowAfterMs: number;
  /** How long a passed check counts as ready. */
  freshForMs: number;
}

export const DEFAULT_READINESS: ReadinessOptions = {
  attemptTimeoutMs: 20_000,
  totalTimeoutMs: 90_000,
  delaysMs: [1_000, 2_000, 4_000, 8_000, 15_000],
  maxAttempts: 8,
  slowAfterMs: 3_000,
  freshForMs: 5 * 60_000,
};

type Listener = () => void;

export class BackendReadiness {
  private phase: ReadinessPhase = "idle";
  private readyAt: number | null = null;
  private running: Promise<boolean> | null = null;
  private generation = 0;
  private controller: AbortController | null = null;
  private readonly listeners = new Set<Listener>();

  constructor(private options: ReadinessOptions = DEFAULT_READINESS) {}

  getPhase = (): ReadinessPhase => this.phase;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Resolves true once the API has answered ready, false when the check gave up. */
  check(): Promise<boolean> {
    if (this.running) return this.running;
    if (
      this.phase === "ready" &&
      this.readyAt !== null &&
      Date.now() - this.readyAt < this.options.freshForMs
    ) {
      return Promise.resolve(true);
    }
    const generation = this.generation;
    this.running = this.attempts(generation).finally(() => {
      if (generation === this.generation) this.running = null;
    });
    return this.running;
  }

  /** A request just failed in a way that suggests the API went away: check again before the
   * next sign-in. */
  markStale(): void {
    this.readyAt = null;
  }

  /** Forget everything and stop a running check (for tests, which may pass shorter timings). */
  reset(options: ReadinessOptions = DEFAULT_READINESS): void {
    this.options = options;
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
    this.running = null;
    this.readyAt = null;
    this.setPhase("idle");
  }

  private setPhase(phase: ReadinessPhase): void {
    if (phase === this.phase) return;
    this.phase = phase;
    this.listeners.forEach((listener) => {
      listener();
    });
  }

  private async attempts(generation: number): Promise<boolean> {
    const { totalTimeoutMs, attemptTimeoutMs, delaysMs, maxAttempts, slowAfterMs } = this.options;
    const current = () => generation === this.generation;
    const deadline = Date.now() + totalTimeoutMs;
    this.setPhase("connecting");
    const slow = setTimeout(() => {
      if (current() && this.phase === "connecting") this.setPhase("waking");
    }, slowAfterMs);
    try {
      for (let attempt = 1; ; attempt += 1) {
        const ok = await this.probe(Math.max(1, Math.min(attemptTimeoutMs, deadline - Date.now())));
        if (!current()) return false;
        if (ok) {
          this.readyAt = Date.now();
          this.setPhase("ready");
          return true;
        }
        const delay = delaysMs[Math.min(attempt - 1, delaysMs.length - 1)] ?? 0;
        if (attempt >= maxAttempts || Date.now() + delay >= deadline) {
          this.setPhase("unavailable");
          return false;
        }
        this.setPhase("waking");
        await new Promise((resolve) => setTimeout(resolve, delay));
        if (!current()) return false;
      }
    } finally {
      clearTimeout(slow);
    }
  }

  private async probe(timeoutMs: number): Promise<boolean> {
    const controller = new AbortController();
    this.controller = controller;
    const timer = setTimeout(() => {
      controller.abort();
    }, timeoutMs);
    try {
      const { data, response } = await client.GET(HEALTH_PATH, { signal: controller.signal });
      return response.ok && data?.status === "ok";
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
      if (this.controller === controller) this.controller = null;
    }
  }
}

/** The one readiness check of this tab. */
export const backendReadiness = new BackendReadiness();

/** The current phase; starts a check when the component mounts, unless one is fresh. */
export function useBackendReadiness(): ReadinessPhase {
  useEffect(() => {
    void backendReadiness.check();
  }, []);
  return useSyncExternalStore(backendReadiness.subscribe, backendReadiness.getPhase);
}
