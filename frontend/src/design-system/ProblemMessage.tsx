import { RotateCw } from "lucide-react";

import { ApiError } from "../lib/problem";
import { Banner } from "./Banner";
import { Button } from "./Button";

/** What failed and what to do, with the request ID for support. Never internal details.
 * With `onRetry`, the person can try again in place. */
export function ProblemMessage({
  error,
  fallback,
  onRetry,
}: {
  error: unknown;
  fallback: string;
  onRetry?: () => void;
}) {
  const message = error instanceof ApiError ? error.message : fallback;
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <Banner tone="danger">
      <p>{message}</p>
      {requestId ? <p className="mt-1 text-xs opacity-80">Reference {requestId}</p> : null}
      {onRetry ? (
        <div className="mt-2">
          <Button variant="secondary" size="sm" onPress={onRetry}>
            <RotateCw aria-hidden className="size-3.5" />
            Try again
          </Button>
        </div>
      ) : null}
    </Banner>
  );
}
