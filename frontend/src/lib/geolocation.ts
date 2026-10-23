/**
 * One reported position for one clock press (ADR-031). Asked only when the person presses a
 * clock button and the server says the press is checked; never in the background. The position
 * goes to the server with the press, which judges it and discards the coordinates. Every way
 * this can fail is reported as such, so the press is still recorded.
 */
import type { components } from "./api-schema";

export type CapturedReport = Omit<components["schemas"]["CapturedLocation"], "sent_at">;
export type FailedReport = components["schemas"]["FailedLocation"];
export type LocationReport = CapturedReport | FailedReport;

// How long the browser may take before the press goes ahead without a position.
export const POSITION_TIMEOUT_MS = 15_000;

export function locate(timeoutMs: number = POSITION_TIMEOUT_MS): Promise<LocationReport> {
  if (!window.isSecureContext || !("geolocation" in navigator)) {
    return Promise.resolve({ status: "unsupported" });
  }
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          status: "captured",
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy_m: position.coords.accuracy,
          captured_at: new Date(position.timestamp).toISOString(),
        });
      },
      (error) => {
        resolve({
          status:
            error.code === error.PERMISSION_DENIED
              ? "permission_denied"
              : error.code === error.TIMEOUT
                ? "timeout"
                : "unavailable",
        });
      },
      { enableHighAccuracy: true, maximumAge: 0, timeout: timeoutMs },
    );
  });
}

/** The report as sent: a captured position carries the device's sending time too. */
export function withSendTime(
  report: LocationReport,
): NonNullable<components["schemas"]["EventCreate"]["location"]> {
  return report.status === "captured" ? { ...report, sent_at: new Date().toISOString() } : report;
}
