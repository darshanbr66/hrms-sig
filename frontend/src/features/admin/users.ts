import type { BadgeTone } from "../../design-system/Badge";
import type { components } from "../../lib/api-schema";

export type User = components["schemas"]["UserView"];

export const USERS_KEY = ["identity", "users"] as const;

export const ACCOUNT_STATUS: Record<User["status"], { label: string; tone: BadgeTone }> = {
  active: { label: "Active", tone: "success" },
  invited: { label: "Invited", tone: "info" },
  disabled: { label: "Disabled", tone: "neutral" },
};
