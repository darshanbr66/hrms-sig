import { CircleCheck, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { backendReadiness, type ReadinessPhase } from "../../lib/readiness";

/** Whether the HRMS can be reached yet. Says "connected" only after the readiness check passed. */
export function ConnectionStatus({ phase }: { phase: ReadinessPhase }) {
  // Nothing for an answer under 300 ms (ui-ux-guidelines.md, States).
  const [patient, setPatient] = useState(false);
  useEffect(() => {
    if (phase !== "connecting") return;
    const timer = setTimeout(() => {
      setPatient(true);
    }, 300);
    return () => {
      clearTimeout(timer);
    };
  }, [phase]);
  const connecting = phase === "waking" || (phase === "connecting" && patient);

  if (phase === "unavailable") {
    return (
      <Banner tone="warning">
        <p>The HRMS is taking longer than usual to start. Wait a moment, then retry.</p>
        <div className="mt-2">
          <Button size="sm" variant="secondary" onPress={() => void backendReadiness.check()}>
            Retry
          </Button>
        </div>
      </Banner>
    );
  }
  return (
    <p role="status" className="flex min-h-5 items-start gap-2 text-sm text-neutral-600">
      {phase === "ready" ? (
        <>
          <CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-success-700" />
          Connected to HRMS
        </>
      ) : !connecting ? null : (
        <>
          <LoaderCircle aria-hidden className="mt-0.5 size-4 shrink-0 animate-spin" />
          <span>
            Connecting to HRMS…
            {phase === "waking" ? (
              <span className="block">
                It is starting up after a quiet period, which can take up to a minute. You can fill in your
                details meanwhile.
              </span>
            ) : null}
          </span>
        </>
      )}
    </p>
  );
}
