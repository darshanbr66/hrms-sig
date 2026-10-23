/**
 * The application's navigation (ui-ux-guidelines.md §2): grouped by whose data the person is
 * working with, and each item shown only when the person holds a permission for it. Hiding an
 * item is cosmetic: the API decides every request (CLAUDE.md rule 1).
 *
 * Every item leads to a working page. Notifications open from the bell in the header.
 */
import {
  Activity,
  BarChart3,
  Building2,
  CalendarClock,
  CalendarCheck,
  CalendarCog,
  CalendarDays,
  CalendarHeart,
  CalendarRange,
  ClipboardCheck,
  ClipboardList,
  Clock,
  ContactRound,
  Eraser,
  FileClock,
  FileText,
  FolderCog,
  KeyRound,
  MapPinned,
  LayoutDashboard,
  type LucideIcon,
  Scale,
  Settings,
  ShieldAlert,
  ShieldCheck,
  UserRound,
  UserRoundCog,
  UserMinus,
  Users,
  UsersRound,
} from "lucide-react";

import { HOME } from "../lib/navigation";
import { hasPermission, type Me } from "./session";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Shown when the person holds any of these; empty means every signed-in person. */
  anyOf: readonly string[];
  /** Matches only the exact path, for items whose path prefixes others. */
  end?: boolean;
}

export interface NavGroup {
  label: string;
  items: readonly NavItem[];
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: "Me",
    items: [
      { to: HOME, label: "Dashboard", icon: LayoutDashboard, anyOf: [], end: true },
      { to: "/attendance", label: "My attendance", icon: Clock, anyOf: ["attendance.clock.self"] },
      { to: "/leave", label: "My leave", icon: CalendarHeart, anyOf: ["leave.request.self"], end: true },
      { to: "/holidays", label: "Holidays", icon: CalendarDays, anyOf: ["holiday.read"] },
      {
        to: "/documents",
        label: "Documents",
        icon: FileText,
        anyOf: ["document.read.self", "document.company.read", "document.company.manage"],
      },
      { to: "/profile", label: "My profile", icon: UserRound, anyOf: ["employee.profile.read.self"] },
      { to: "/account/security", label: "Account security", icon: ShieldCheck, anyOf: [] },
    ],
  },
  {
    label: "Team",
    items: [
      {
        to: "/team/attendance",
        label: "Team attendance",
        icon: UsersRound,
        anyOf: ["attendance.read.team", "attendance.read.all"],
      },
      {
        to: "/attendance/corrections",
        label: "Attendance corrections",
        icon: ClipboardCheck,
        anyOf: ["attendance.correction.approve.team", "attendance.correction.approve.all"],
        end: true,
      },
      {
        to: "/attendance/location-reviews",
        label: "Location reviews",
        icon: MapPinned,
        anyOf: ["attendance.location.review.all"],
      },
      {
        to: "/leave/approvals",
        label: "Leave approvals",
        icon: CalendarCheck,
        anyOf: ["leave.request.approve.team", "leave.request.approve.all"],
      },
      {
        to: "/leave/calendar",
        label: "Leave calendar",
        icon: CalendarDays,
        anyOf: ["leave.read.team", "leave.read.all"],
      },
    ],
  },
  {
    label: "People",
    items: [
      {
        to: "/employees",
        label: "Employees",
        icon: Users,
        anyOf: ["employee.profile.read.team", "employee.profile.read.all"],
      },
      {
        to: "/former-employees",
        label: "Former employees",
        icon: UserMinus,
        anyOf: ["employee.profile.read.all"],
      },
      { to: "/directory", label: "Directory", icon: ContactRound, anyOf: ["employee.directory.read"] },
      { to: "/organization", label: "Organization", icon: Building2, anyOf: ["org.read"] },
      { to: "/leave/overview", label: "Leave overview", icon: ClipboardList, anyOf: ["leave.read.all"] },
      { to: "/leave/balances", label: "Leave balances", icon: Scale, anyOf: ["leave.balance.adjust"] },
      {
        to: "/reports",
        label: "Reports",
        icon: BarChart3,
        anyOf: [
          "leave.read.team",
          "leave.read.all",
          "attendance.export",
          "leave.export",
          "employee.export",
          "audit.export",
        ],
      },
    ],
  },
  {
    label: "Time setup",
    items: [
      {
        to: "/setup/holiday-calendars",
        label: "Holiday calendars",
        icon: CalendarRange,
        anyOf: ["holiday.manage"],
      },
      { to: "/setup/leave-types", label: "Leave types", icon: CalendarCog, anyOf: ["leave.policy.manage"] },
      {
        to: "/setup/attendance",
        label: "Attendance set-up",
        icon: CalendarClock,
        anyOf: ["shift.manage", "attendance.policy.manage", "shift.assign.all"],
      },
      {
        to: "/setup/document-categories",
        label: "Document categories",
        icon: FolderCog,
        anyOf: ["document.category.manage"],
      },
    ],
  },
  {
    label: "Administration",
    items: [
      { to: "/admin/users", label: "Users", icon: UserRoundCog, anyOf: ["user.read.all"] },
      { to: "/admin/roles", label: "Roles", icon: KeyRound, anyOf: ["role.read"] },
      { to: "/admin/access-requests", label: "Access requests", icon: ShieldAlert, anyOf: ["role.read"] },
      { to: "/admin/erasures", label: "Data erasure", icon: Eraser, anyOf: ["employee.erasure.read"] },
      {
        to: "/admin/security-events",
        label: "Security events",
        icon: Activity,
        anyOf: ["security.event.read"],
      },
      { to: "/admin/audit-log", label: "Audit log", icon: FileClock, anyOf: ["audit.read"] },
      { to: "/settings", label: "Settings", icon: Settings, anyOf: ["settings.read"] },
    ],
  },
];

export function canSee(me: Me | null | undefined, item: Pick<NavItem, "anyOf">): boolean {
  return item.anyOf.length === 0 || item.anyOf.some((permission) => hasPermission(me, permission));
}

/** The groups and items this person may open, without empty groups. */
export function visibleNavGroups(me: Me | null | undefined): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => canSee(me, item)),
  })).filter((group) => group.items.length > 0);
}

export function visibleNavItems(me: Me | null | undefined): NavItem[] {
  return visibleNavGroups(me).flatMap((group) => group.items);
}

/** The page title for the current path, for the header and the browser tab. */
export function titleFor(pathname: string): string {
  const items = NAV_GROUPS.flatMap((group) => group.items);
  const exact = items.find((item) => item.to === pathname);
  if (exact) return exact.label;
  if (pathname === "/leave/request") return "Request leave";
  if (pathname === "/notifications") return "Notifications";
  if (pathname.startsWith("/attendance/corrections/")) return "Attendance correction";
  if (pathname.startsWith("/leave/requests/")) return "Leave request";
  const prefixed = items
    .filter((item) => item.to !== "/" && pathname.startsWith(item.to))
    .sort((a, b) => b.to.length - a.to.length)[0];
  return prefixed?.label ?? "Sigvitas HRMS";
}
