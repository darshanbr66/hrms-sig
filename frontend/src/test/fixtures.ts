/** Obviously fake API data for component tests (docs/database-design.md §10). */
import type { Me } from "../app/session";
import type { components } from "../lib/api-schema";
import { ME } from "./fakeApi";

type Schemas = components["schemas"];

export const EMPLOYEE_ID = "01900000-0000-7000-8000-0000000000e1";
export const MANAGER_ID = "01900000-0000-7000-8000-0000000000e2";
export const DEPARTMENT = { id: "01900000-0000-7000-8000-0000000000d1", name: "Test department" };
export const DESIGNATION = { id: "01900000-0000-7000-8000-0000000000d2", name: "Test designation" };
export const LOCATION = { id: "01900000-0000-7000-8000-0000000000d3", name: "Test location" };

const EMPLOYEE_PERMISSIONS = [
  "auth.session.read.self",
  "auth.session.revoke.self",
  "org.read",
  "employee.directory.read",
  "employee.profile.read.self",
  "employee.profile.update.self",
  "employee.personal.read.self",
  "employee.personal.update.self",
  "attendance.clock.self",
  "attendance.read.self",
  "holiday.read",
];

export const EMPLOYEE_ME: Me = {
  ...ME,
  employee_id: EMPLOYEE_ID,
  roles: ["employee"],
  permissions: EMPLOYEE_PERMISSIONS,
};

export const MANAGER_ME: Me = {
  ...EMPLOYEE_ME,
  roles: ["employee", "manager"],
  permissions: [...EMPLOYEE_PERMISSIONS, "employee.profile.read.team", "attendance.read.team"],
};

export const HR_ADMIN_ME: Me = {
  ...ME,
  roles: ["hr_admin"],
  permissions: [
    "auth.session.read.self",
    "auth.session.revoke.self",
    "org.read",
    "org.manage",
    "holiday.read",
    "holiday.manage",
    "user.read.all",
    "user.invite",
    "user.disable",
    "employee.profile.read.all",
    "employee.profile.update.all",
    "employee.personal.read.all",
    "employee.personal.update.all",
    "employee.create",
    "employee.lifecycle.manage",
    "attendance.read.all",
    "role.read",
    "settings.read",
  ],
};

export const SUPER_ADMIN_ME: Me = {
  ...ME,
  roles: ["super_admin"],
  permissions: [
    "auth.session.read.self",
    "auth.session.revoke.self",
    "user.read.all",
    "security.event.read",
    "role.read",
    "role.assign",
    "role.elevation.request",
    "role.elevation.approve",
    "role.elevation.break_glass",
    "audit.read",
    "settings.read",
  ],
};

export function currentJob(overrides: Partial<Schemas["CurrentJob"]> = {}): Schemas["CurrentJob"] {
  return {
    department: DEPARTMENT,
    designation: DESIGNATION,
    location: LOCATION,
    manager: { id: MANAGER_ID, name: "Test Manager" },
    employment_type: "full_time",
    effective_from: "2026-01-05",
    ...overrides,
  };
}

export function employee(overrides: Partial<Schemas["EmployeeView"]> = {}): Schemas["EmployeeView"] {
  return {
    id: EMPLOYEE_ID,
    employee_code: "TEST-0001",
    name: "Test Person",
    legal_first_name: "Test",
    legal_last_name: "Person",
    preferred_name: null,
    work_email: "test.person@dev.example",
    work_phone: null,
    status: "active",
    date_of_joining: "2026-01-05",
    date_of_exit: null,
    job: currentJob(),
    version: 1,
    created_at: "2026-01-01T09:00:00Z",
    updated_at: "2026-01-01T09:00:00Z",
    ...overrides,
  };
}

export function summary(overrides: Partial<Schemas["EmployeeSummary"]> = {}): Schemas["EmployeeSummary"] {
  return {
    id: EMPLOYEE_ID,
    employee_code: "TEST-0001",
    name: "Test Person",
    preferred_name: null,
    work_email: "test.person@dev.example",
    status: "active",
    date_of_joining: "2026-01-05",
    job: currentJob(),
    ...overrides,
  };
}

export function today(overrides: Partial<Schemas["TodayView"]> = {}): Schemas["TodayView"] {
  return {
    state: "clocked_out",
    work_date: "2026-10-05",
    time_zone: "Asia/Kolkata",
    server_time: "2026-10-05T03:30:00Z",
    allowed_events: ["CLOCK_IN"],
    open_since: null,
    day: {
      work_date: "2026-10-05",
      first_in: null,
      last_out: null,
      session_seconds: 0,
      break_seconds: 0,
      open_session: false,
      on_break: false,
      missing_break_end: false,
      events: [],
    },
    ...overrides,
  };
}

export const STATISTICS: Schemas["EmployeeStatistics"] = {
  employed: 42,
  active: 40,
  on_notice: 2,
  pre_joining: 3,
  joined_last_30_days: 5,
  starting_soon: [{ employee: { id: MANAGER_ID, name: "Test Joiner" }, date_of_joining: "2026-10-20" }],
  work_anniversaries: [],
};
