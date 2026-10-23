import type { BadgeTone } from "../../design-system/Badge";
import type { components } from "../../lib/api-schema";

export type User = components["schemas"]["UserView"];

export const USERS_KEY = ["identity", "users"] as const;

export const ACCOUNT_STATUS: Record<User["status"], { label: string; tone: BadgeTone }> = {
  active: { label: "Active", tone: "success" },
  invited: { label: "Invited", tone: "info" },
  disabled: { label: "Disabled", tone: "neutral" },
};

/**
 * Why a disabled account can never be enabled again through Enable account (the API decides,
 * `enable_blocked`): the person has left, or their data was permanently erased. An ordinarily
 * disabled account has neither and can be enabled again.
 */
export const ENABLE_BLOCKED: Record<
  NonNullable<User["enable_blocked"]>,
  { badge: string; explanation: string }
> = {
  employee_exited: {
    badge: "Person has left",
    explanation:
      "This person has been offboarded, so their account stays disabled and cannot be enabled here. Their records are kept. There is no rehire workflow yet; a returning person needs a new employee record and account.",
  },
  erased: {
    badge: "Data erased",
    explanation:
      "This person's data was permanently erased. The account is disabled for good: it cannot be enabled, restored or linked to anyone again.",
  },
};
