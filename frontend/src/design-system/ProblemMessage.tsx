import { ApiError } from "../lib/problem";
import { Banner } from "./Banner";

/** What failed and what to do, with the request ID for support. Never internal details. */
export function ProblemMessage({ error, fallback }: { error: unknown; fallback: string }) {
  const message = error instanceof ApiError ? error.message : fallback;
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <Banner tone="danger">
      <p>{message}</p>
      {requestId ? <p className="mt-1 text-xs opacity-80">Reference {requestId}</p> : null}
    </Banner>
  );
}
