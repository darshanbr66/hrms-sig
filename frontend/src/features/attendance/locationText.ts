/**
 * The words for office location checks (ADR-031), in one place. They say "reported location":
 * a browser position is evidence for review, never proof of where someone was or who pressed.
 */
import type { BadgeTone } from "../../design-system/Badge";
import type { components } from "../../lib/api-schema";

export type LocationOutcome = components["schemas"]["EventLocationView"]["outcome"];

export const OUTCOME_LABELS: Record<LocationOutcome, { label: string; tone: BadgeTone }> = {
  verified: { label: "Reported location matched", tone: "success" },
  outside_radius: { label: "Reported location outside the office", tone: "warning" },
  inconclusive: { label: "Reported location inconclusive", tone: "warning" },
  unverified: { label: "Location not checked", tone: "neutral" },
};

export const REASON_LABELS: Record<string, string> = {
  low_accuracy: "The reported position was not precise enough",
  boundary: "The reported position was too close to the edge of the office area to tell",
  permission_denied: "Location access was blocked",
  unavailable: "The device could not find its location",
  timeout: "Finding the location took too long",
  unsupported: "The browser cannot share its location",
  skipped: "Continued without location",
  not_provided: "No location was sent",
  stale_position: "The reported position was too old",
  invalid_timestamp: "The device's clock gave an impossible time",
};

/** What the person is told after pressing, about their own event. */
export function outcomeMessage(location: { outcome: LocationOutcome; reason_codes: string[] }): string {
  const recorded = "Your attendance was recorded; HR may review the location check.";
  switch (location.outcome) {
    case "verified":
      return "Your reported location was within an approved office.";
    case "outside_radius":
      return `Your reported location was outside your approved offices. ${recorded}`;
    case "inconclusive":
      return `${REASON_LABELS[location.reason_codes[0] ?? "low_accuracy"] ?? "The check was inconclusive"}, so the check is inconclusive. ${recorded}`;
    case "unverified": {
      const reason = location.reason_codes[0] ?? "not_provided";
      const help =
        reason === "permission_denied"
          ? " To allow it next time, change this site's location permission in your browser settings."
          : "";
      return `${REASON_LABELS[reason] ?? "Your location was not checked"}, so your location was not checked. ${recorded}${help}`;
    }
  }
}

/**
 * The privacy notice shown before the first location request. A draft: the designated
 * privacy/compliance owner must approve it, and the retention period it names must be decided,
 * before location checks are turned on in production (docs/module-acceptance.md §15).
 */
export const PRIVACY_NOTICE = [
  "What is collected: when you press Clock in, Start break, End break or Clock out, your browser is asked once for its reported position and how accurate it is. Nothing is collected at any other time, and never in the background.",
  "Why: to check whether the press was made at one of the offices you are approved to work at. A mismatch is never a reason to refuse your attendance; it is flagged for HR to review.",
  "What is kept: only the result of the check, the distance to the office rounded to 10 metres, the reported accuracy and when it was received. Your exact position is used for the check and then discarded; it is never stored.",
  "Who can see it: you can see the result for your own clock events. HR and HR admins can review results within the people their role covers. Managers cannot see them. Nobody can review their own.",
  "How long it is kept: the retention period for these records has not been decided yet. It will be stated here before location checks are turned on.",
  "Your choice: you can block location access in your browser or continue without location. Your attendance is still recorded and the event is marked as not checked.",
  "Limits: a browser position can be wrong, especially on laptops, and can be changed by someone who tries. It is evidence for review, not proof of where anyone was.",
] as const;
