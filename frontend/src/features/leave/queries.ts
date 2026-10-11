/** Leave queries, types and labels shared by the leave pages. Day counts and balances come from
 * the API as decimal strings; the pages format them, never compute them (CLAUDE.md rule 14). */
import { useQuery } from "@tanstack/react-query";

import type { BadgeTone } from "../../design-system/Badge";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";

type Schemas = components["schemas"];
export type LeaveType = Schemas["LeaveTypeView"];
export type LeavePolicy = Schemas["LeavePolicyView"];
export type Balance = Schemas["BalanceView"];
export type LedgerEntry = Schemas["LedgerEntryView"];
export type RequestSummary = Schemas["RequestSummary"];
export type RequestDetail = Schemas["RequestDetail"];
export type RequestStatus = RequestSummary["status"];

export const LEAVE_KEY = ["leave"] as const;

export const STATUS_LABELS: Record<RequestStatus, string> = {
  pending: "Waiting for approval",
  approved: "Approved",
  rejected: "Not approved",
  cancelled: "Cancelled",
  cancellation_requested: "Cancellation asked",
};

export const STATUS_TONES: Record<RequestStatus, BadgeTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
  cancelled: "neutral",
  cancellation_requested: "info",
};

export const ENTRY_LABELS: Record<LedgerEntry["entry_type"], string> = {
  opening_balance: "Opening balance",
  accrual: "Allocation",
  usage: "Leave taken",
  usage_reversal: "Leave cancelled",
  adjustment: "Adjustment",
};

/** "1.50" -> "1.5 days", "1.00" -> "1 day". */
export function formatDays(value: string): string {
  const number = Number(value);
  const text = Number.isInteger(number) ? String(number) : number.toFixed(1);
  return `${text} ${Math.abs(number) === 1 ? "day" : "days"}`;
}

/** The dates of a request, with the halves it takes. */
export function formatSpan(item: {
  start_date: string;
  end_date: string;
  start_half: "first" | "second" | null;
  end_half: "first" | "second" | null;
}): string {
  const day = (value: string) =>
    new Date(`${value}T00:00:00`).toLocaleDateString(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  const half = (value: "first" | "second" | null) =>
    value === "first" ? " (morning)" : value === "second" ? " (afternoon)" : "";
  if (item.start_date === item.end_date)
    return `${day(item.start_date)}${half(item.start_half ?? item.end_half)}`;
  return `${day(item.start_date)}${half(item.start_half)} to ${day(item.end_date)}${half(item.end_half)}`;
}

export function useLeaveTypes(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...LEAVE_KEY, "types", includeArchived],
    queryFn: () =>
      unwrap(client.GET("/api/v1/leave-types", { params: { query: { include_archived: includeArchived } } })),
    enabled,
  });
}

/** Balances of the signed-in employee, or of one employee for HR and managers. */
export function useBalances(employeeId: string | null) {
  return useQuery({
    queryKey: [...LEAVE_KEY, "balances", employeeId ?? "me"],
    queryFn: () =>
      employeeId
        ? unwrap(
            client.GET("/api/v1/employees/{employee_id}/leave/balances", {
              params: { path: { employee_id: employeeId } },
            }),
          )
        : unwrap(client.GET("/api/v1/me/leave/balances")),
  });
}

export function useLedger(employeeId: string | null) {
  return useQuery({
    queryKey: [...LEAVE_KEY, "ledger", employeeId ?? "me"],
    queryFn: () =>
      employeeId
        ? unwrap(
            client.GET("/api/v1/employees/{employee_id}/leave/ledger", {
              params: { path: { employee_id: employeeId } },
            }),
          )
        : unwrap(client.GET("/api/v1/me/leave/ledger")),
  });
}
