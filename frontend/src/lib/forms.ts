import type { FieldValues, Path, UseFormSetError } from "react-hook-form";

import { ApiError } from "./problem";

/** Put the API's field errors on the form's fields (ui-ux-guidelines.md §6, Forms).
 * Returns true when at least one error was placed on a known field. */
export function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  fields: readonly Path<T>[],
): boolean {
  if (!(error instanceof ApiError) || !error.is("validation-error")) {
    return false;
  }
  let placed = false;
  for (const fieldError of error.problem.errors ?? []) {
    const field = fields.find((name) => name === fieldError.field);
    if (field) {
      setError(field, { type: "server", message: fieldError.message });
      placed = true;
    }
  }
  return placed;
}

/** User-facing text for problems every sign-in form can meet. */
export function signInProblemText(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "We couldn't sign you in. Try again.";
  }
  if (error.is("rate-limited")) {
    const minutes = Math.max(1, Math.ceil((error.retryAfterSeconds ?? 60) / 60));
    const unit = minutes === 1 ? "minute" : "minutes";
    return `Too many sign-in attempts from this network. Wait ${minutes} ${unit}, then try again.`;
  }
  if (error.is("temporarily-unavailable")) {
    return "Sign-in is temporarily unavailable. Try again in a minute.";
  }
  return error.message;
}
