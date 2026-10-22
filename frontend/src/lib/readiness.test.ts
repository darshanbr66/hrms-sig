import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BackendReadiness, HEALTH_PATH, type ReadinessOptions, type ReadinessPhase } from "./readiness";

const OPTIONS: ReadinessOptions = {
  attemptTimeoutMs: 10_000,
  totalTimeoutMs: 60_000,
  delaysMs: [1_000, 2_000, 4_000],
  maxAttempts: 6,
  slowAfterMs: 3_000,
  freshForMs: 300_000,
};

const ok = () => new Response(JSON.stringify({ status: "ok" }), { status: 200 });
const unavailable = () =>
  new Response(JSON.stringify({ type: "/problems/temporarily-unavailable" }), { status: 503 });

/** A fetch that answers from `replies` in turn (the last keeps answering) and records each call. */
function stubFetch(...replies: (() => Response | Promise<Response>)[]) {
  const calls: { url: string; at: number; signal: AbortSignal | null }[] = [];
  // The API client passes a Request.
  vi.stubGlobal("fetch", (request: Request) => {
    calls.push({ url: new URL(request.url).pathname, at: Date.now(), signal: request.signal });
    const reply = replies.length > 1 ? replies.shift() : replies[0];
    return Promise.resolve(reply ? reply() : ok());
  });
  return calls;
}

/** A request that never answers until it is aborted, like a sleeping API behind a proxy. */
function hanging(signalOf: () => AbortSignal | null) {
  return () =>
    new Promise<Response>((_resolve, reject) => {
      signalOf()?.addEventListener("abort", () => {
        reject(new DOMException("aborted", "AbortError"));
      });
    });
}

function phases(readiness: BackendReadiness): ReadinessPhase[] {
  const seen: ReadinessPhase[] = [];
  readiness.subscribe(() => seen.push(readiness.getPhase()));
  return seen;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("backend readiness", () => {
  it("is ready after one passing check, and reuses it while it is fresh", async () => {
    const calls = stubFetch(ok);
    const readiness = new BackendReadiness(OPTIONS);
    const seen = phases(readiness);
    expect(readiness.getPhase()).toBe("idle");

    await expect(readiness.check()).resolves.toBe(true);
    expect(seen).toEqual(["connecting", "ready"]);
    expect(calls.map((call) => call.url)).toEqual([HEALTH_PATH]);

    await vi.advanceTimersByTimeAsync(OPTIONS.freshForMs - 1);
    await expect(readiness.check()).resolves.toBe(true);
    expect(calls).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(1);
    await expect(readiness.check()).resolves.toBe(true);
    expect(calls).toHaveLength(2);
  });

  it("shares one running check between callers", async () => {
    const calls = stubFetch(ok);
    const readiness = new BackendReadiness(OPTIONS);
    const results = await Promise.all([readiness.check(), readiness.check(), readiness.check()]);
    expect(results).toEqual([true, true, true]);
    expect(calls).toHaveLength(1);
  });

  it("retries with growing pauses until the API answers ready", async () => {
    const calls = stubFetch(
      unavailable,
      () => Promise.reject(new TypeError("Failed to fetch")),
      unavailable,
      ok,
    );
    const readiness = new BackendReadiness(OPTIONS);
    const seen = phases(readiness);
    const started = Date.now();
    const result = readiness.check();

    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toHaveLength(1);
    expect(readiness.getPhase()).toBe("waking");
    await vi.advanceTimersByTimeAsync(999);
    expect(calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(result).resolves.toBe(true);
    expect(calls.map((call) => call.at - started)).toEqual([0, 1_000, 3_000, 7_000]);
    expect(seen).toEqual(["connecting", "waking", "ready"]);
  });

  it("does not count an answer other than status ok as ready", async () => {
    stubFetch(() => new Response(JSON.stringify({ items: [] }), { status: 200 }), ok);
    const readiness = new BackendReadiness(OPTIONS);
    const result = readiness.check();
    await vi.advanceTimersByTimeAsync(0);
    expect(readiness.getPhase()).toBe("waking");
    await vi.advanceTimersByTimeAsync(1_000);
    await expect(result).resolves.toBe(true);
  });

  it("says it is starting up when the first answer is slow", async () => {
    let release: (response: Response) => void = () => undefined;
    stubFetch(() => new Promise<Response>((resolve) => (release = resolve)));
    const readiness = new BackendReadiness(OPTIONS);
    const result = readiness.check();
    await vi.advanceTimersByTimeAsync(OPTIONS.slowAfterMs - 1);
    expect(readiness.getPhase()).toBe("connecting");
    await vi.advanceTimersByTimeAsync(1);
    expect(readiness.getPhase()).toBe("waking");
    release(ok());
    await expect(result).resolves.toBe(true);
    expect(readiness.getPhase()).toBe("ready");
  });

  it("abandons a request that hangs, and gives up within the overall time limit", async () => {
    const calls: { url: string; at: number; signal: AbortSignal | null }[] = stubFetch(
      hanging(() => calls.at(-1)?.signal ?? null),
    );
    const readiness = new BackendReadiness(OPTIONS);
    const started = Date.now();
    const result = readiness.check();
    const settled: { at: number | null } = { at: null };
    void result.then(() => (settled.at = Date.now()));

    await vi.advanceTimersByTimeAsync(OPTIONS.attemptTimeoutMs - 1);
    expect(calls[0]?.signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls[0]?.signal?.aborted).toBe(true);

    await vi.advanceTimersByTimeAsync(OPTIONS.totalTimeoutMs);
    expect(settled.at).not.toBeNull();
    await expect(result).resolves.toBe(false);
    expect(readiness.getPhase()).toBe("unavailable");
    // 10 s + 1 s pause + 10 s + 2 s + 10 s + 4 s + 10 s + 4 s + 10 s = 61 s > 60 s: the fifth
    // attempt is cut short at the deadline, and no sixth starts.
    expect(calls.map((call) => call.at - started)).toEqual([0, 11_000, 23_000, 37_000, 51_000]);
    expect((settled.at ?? Infinity) - started).toBeLessThanOrEqual(OPTIONS.totalTimeoutMs);
  });

  it("stops after the last attempt and sends nothing more until asked again", async () => {
    const calls = stubFetch(unavailable);
    const readiness = new BackendReadiness({ ...OPTIONS, maxAttempts: 3 });
    const result = readiness.check();
    await vi.advanceTimersByTimeAsync(OPTIONS.totalTimeoutMs);
    await expect(result).resolves.toBe(false);
    expect(calls).toHaveLength(3);
    expect(readiness.getPhase()).toBe("unavailable");

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(calls).toHaveLength(3);

    // Retry starts a new, equally bounded check.
    stubFetch(ok);
    await expect(readiness.check()).resolves.toBe(true);
    expect(readiness.getPhase()).toBe("ready");
  });

  it("checks again after a request suggests the API went away", async () => {
    const calls = stubFetch(ok);
    const readiness = new BackendReadiness(OPTIONS);
    await readiness.check();
    readiness.markStale();
    await readiness.check();
    expect(calls).toHaveLength(2);
  });
});
