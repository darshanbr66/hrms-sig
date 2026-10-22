# Module acceptance

How a Sigvitas HRMS module is judged complete, the state of every module against that bar, and the browser scripts used to accept them. Sigvitas HRMS is an HR management system: no candidate, application or hiring-pipeline module is in scope.

Fix and finish the modules below before starting new ones. Update this page in the same change that moves a module forward.

## 1. Definition of complete

A module is complete only when every gate below has evidence. A page that renders, or a green test run on its own, is not evidence of a working workflow.

| Gate | Evidence required |
|---|---|
| Backend | Every endpoint in `api-architecture.md` for the module exists, uses the module layout and goes through the service layer. |
| Frontend | Every action has a working control: no dead buttons, no dialog that cannot open, loading, empty, error and retry states on every list and form. Every dialog uses the shared `Dialog` with its buttons in `DialogActions`, and each add dialog is in `frontend/e2e/dialogs.e2e.ts` (fits small laptop, desktop, 200 % zoom and phone screens). The record actions in §9 are offered where permitted, with their consequences explained before an archiving or removing action. Main screens pass axe in the light and the dark theme (`frontend/e2e/theme.e2e.ts`). |
| Persistence | Migration applied by Alembic; constraints match `database-design.md`; the schema-drift test passes; data survives a page refresh. |
| Authorization | Every route declares a permission; list queries take the scope filter; the authorization matrix and route-coverage tests include the routes; a holder without the permission gets `403` (or `404` outside scope). |
| Audit | Every change writes an audit row in the same transaction, with old and new values or a redaction marker; sensitive values are referenced, not copied. |
| Error handling | Validation errors return RFC 9457 problems with field names, shown beside the field; conflicts and stale versions (`409`, `412`) say what to do next. |
| Tests | Backend integration tests for the success path, invalid input, duplicates, refusals and concurrency where relevant; frontend tests for each dialog **including the empty state**; axe on the main page. |
| Browser workflow | A person signed in with the right role walks the script in §3 on a named local database, including a refresh after each save, and records the date, database and account. |
| Documentation | Requirement IDs, permissions, schema and API documents describe what was built. |

The empty-state rule exists because of a real defect: on 2026-10-16 the Organization page's Add buttons did nothing while a list was empty (see §4).

## 2. Status by module

Legend: **Yes** = evidence exists (automated tests, recorded in `m1-implementation-plan.md` §12); **No** = not done; **n/a** = not applicable. No module has a recorded browser acceptance by engineering yet: signing in needs the owner's authenticator. What is verified in a real browser is layout only: `pnpm test:e2e` opens the add dialogs in Chromium with fake API data. On 2026-10-16 the `hrms_local_test` audit log shows the owner created organization records, an employee and a linked, activated account through the application; that is evidence for §3.1 and §3.2, not a recorded acceptance.

| Module | Backend | Frontend | Persistence | Authz | Audit | Tests | Browser | Notes |
|---|---|---|---|---|---|---|---|---|
| Sign-in, MFA, step-up, sessions, devices | Yes | Yes | Yes | Yes | Yes | Yes | No | Passkeys are Phase 2. |
| Invites, password reset, MFA reset | Yes | Yes | Yes | Yes | Yes | Yes | No | Email through the outbox to Mailpit locally. Staff provisioning journey tested end to end; script §3.3. |
| Roles, elevation, break-glass | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Audit log, sealer, checkpoints, retention | Yes | Yes | Yes | Yes | n/a | Yes | No | |
| Settings | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Organization (locations, departments, designations) | Yes | Yes (fixed 2026-10-16) | Yes | Yes | Yes | Yes | No | Script §3.1. Field errors from request validation use the validator's generic text ("String should have at least 1 character"), not product wording; applies to every module. |
| Employee records, job history, status, exit | Yes | Yes | Yes | Yes | Yes | Yes | No | Script §3.2. Identifiers and bank accounts not built (Sigvitas input). |
| Personal tier, emergency contacts, self-service, directory | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Account linking | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Attendance events, team today | Yes | Yes | Yes | Yes | Yes | Yes | No | Location controls not built, see §6. |
| Shifts, attendance policies, day status | Yes | Yes | Yes | Yes | Yes | Yes | No | Policy preview and overtime approval not built. Workweek and working Saturdays: §7.1, script §3.4. |
| Attendance corrections and locks | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Holiday calendars, declared working days | Yes | Yes | Yes (0027 not yet applied to `hrms_local_test`) | Yes | Yes | Yes | No | Scripts §3.4 and §3.5. Entries editable (name, optional); calendars renamed, archived only when no location uses them, restored (2026-10-16, no migration). |
| Leave types, policies, ledger, requests, approvals | Yes | Yes | Yes | Yes | Yes | Yes | No | Attachments, multi-level approval and encashment not built; policy decisions open, §7.2. |
| Leave accrual, year end, opening-balance import | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Documents and scanning | Yes | Yes | Yes | Yes | Yes | Yes | No | EXIF stripping and document retention not built. |
| Notifications | Yes | Yes | Yes | Yes | n/a | Yes | No | |
| Reports and exports | Yes | Yes | Yes | Yes | Yes | Yes | No | |
| Dashboard | Yes | Yes | n/a | Yes | n/a | Yes | No | |
| Compensation, payroll, payslips | No | No | No | No | No | No | No | Phase 2, see §8. |

## 3. Browser acceptance scripts

Run on `hrms_local_test` (never `hrms`, `hrms_acceptance` or `hrms_m1verify`), with obviously fake data whose names start with `Test`. After each save, refresh the page and confirm the change is still there. Record the outcome in §2.

### 3.1 Organization (HR admin)

Needs an account holding `hr_admin` (or a super admin with an approved, unexpired `hr_admin` elevation).

1. Organization → Overview shows "Set up your organization" with an action per missing record type.
2. Locations → **Add location** opens the dialog while the list is empty. Save with an empty code: the API's message appears under Code (today it is the generic validator text, see §2 notes). Save with time zone `Nowhere/Test`: the time zone field says to choose one such as Asia/Kolkata.
3. Add `TEST-LOC` / Test office / `Asia/Kolkata` / `in`: the toast says "Location added", the country shows `IN`, the record is still there after a refresh.
4. Add a second location with code `test-loc`: refused with "Another record already uses this code."
5. Departments → add `TEST-DEP` / Test team. Add `TEST-SUB` / Test sub-team with **Part of** Test team. Edit Test team and choose Test sub-team as its parent: refused, a department cannot sit inside its own team.
6. Archive Test team while Test sub-team is active: refused with what to do first. Archive Test sub-team, then Test team: both succeed; **Show archived** lists them; un-archive Test team by setting the status back to Active.
7. Designations → add `TEST-DES` / Test engineer, level `1`. Try level `101`: refused beside the field.
8. Edit a record in two tabs and save both: the second save says "This record changed after you opened it. Reload it and try again."
9. Audit log (as a holder of `audit.read`): `org.location.created`, `org.department.created`, `org.department.updated`, `org.designation.created` with the account as actor.
10. Signed in as an employee: Organization lists are visible with no Add or Edit buttons.

### 3.2 First employee, invite and activation

1. As the HR admin: Employees → **Add employee** with `TEST-0001`, Test / Starter, work email `employee.test@example.com`, today's date, the test department, designation and location. It appears in the Employees list; the record's Job history shows one `joining` row with those three records.
2. On the record, **HRMS account** → send the invite. Mailpit (http://127.0.0.1:8025) holds one email to `employee.test@example.com` with an invite link.
3. In a private window open the link: set a password; the next screen requires an authenticator (scan the QR code with a test entry, confirm a code); there is no way to skip it. Save the recovery codes in the test password manager.
4. Signed in as the new employee: the navigation matches the `employee` row in `roles-and-permissions.md`. `/employees` lists only their own record, `/users` is refused, and `/organization?tab=departments` shows no Add or Edit button.
5. My profile shows their job; **Clock in** on My attendance records a `CLOCK_IN` (signing in did not). Sign out and in again: the attendance state is unchanged.

The API half of this script is automated in `backend/tests/integration/test_onboarding_journey.py`.

### 3.3 HR and HR admin accounts (two super admins)

The supported route; no step writes to the database directly. The full rules are in `roles-and-permissions.md` §6.

1. Someone holding `user.invite` invites the person's email: a `system_admin`, an `hr` or `hr_admin` account, or a super admin with an approved, unexpired elevation to `system_admin` or `hr_admin`. A super admin without an elevation gets "You don't have access to this." Users → Invite, no employee record for a staff-only account; Employee record → HRMS account for an employee (the invite email must be the work email).
2. The person opens the invite from Mailpit in a private window, sets a password, enrols an authenticator and saves the recovery codes. Before a role is assigned they see only their own account pages.
3. A super admin (not the person themselves) opens Users → the account → Roles, assigns `hr` or `hr_admin` with a reason, and confirms with an authenticator code. Assigning while elevated is allowed; assigning to oneself is refused.
4. The person refreshes: HR sees employee, attendance and leave operations; an HR admin also sees Organization with Add buttons, Attendance set-up, Leave types and Holiday calendars. Neither sees Roles management.
5. Audit log (as a super admin): `identity.user.invited`, `identity.user.activated`, `access.role.assigned` with the reason.
6. Disable a test account from Users → the account (step-up): its open browser session ends at once.

### 3.4 Workweek and an announced working Saturday (HR admin)

Shift times, weekly offs and policy values below are placeholders for the test only, not Sigvitas policy.

1. Attendance set-up → Shifts → Add shift `TEST-WK` with test times and weekly offs Saturday and Sunday. Location defaults → the test location uses it. Policies → add a test policy version for the location.
2. Holiday calendars → a calendar for the year, assigned to the test location → **Add a date** → Type "Working day on a usual day off", a Saturday, name "Test working Saturday". The list shows it with a Working day badge; the optional-holiday choice is not offered.
3. As the test employee: Holidays lists the Saturday as a Working day. My attendance for that Saturday (once it has passed) is judged as a working day; the Saturdays before and after are weekly offs.
4. Request leave over a week containing the Saturday with a leave type that does not count weekly offs: the preview counts the Saturday and leaves out the Sunday.
5. Remove the working day from the calendar: the Saturday is a weekly off again. Audit log: `holiday.added` and `holiday.removed` with `kind`.

### 3.5 Holiday calendar actions (HR admin)

1. Holiday calendars: each row has an **Open** action (and the name is a link); the list shows holidays and working days separately. Open a calendar used by a location.
2. **Edit** a holiday: change its name and tick Optional; save. The toast says "Holiday updated"; the table shows the new name and the Optional badge. The dialog explains that the date and type do not change. Edit a working day: no Optional choice.
3. **Remove** a holiday: the confirmation says the date stops being a holiday for the calendar's locations, past attendance and leave counts included. Confirm; refresh: it is gone.
4. **Archive** while a location uses the calendar: the confirmation says it will be refused, and the API's reason is shown. Remove the location (or give it another calendar), then archive: a banner says the calendar is archived; Add a date, Edit, Remove and the location picker are gone. **Restore calendar** brings them back.
5. **Rename** the calendar; refresh: the new name shows in the list and the page title.
6. Audit log: `holiday.updated` (with the entry's date and kind), `holiday.removed`, and `holiday.calendar.updated` for the rename, the archive and the restore.
7. As HR (not HR admin): Holiday calendars is not in the navigation, and a direct `PATCH` to an entry is refused with `403`.

## 4. Organization defect of 2026-10-16

Symptom: on `/organization?tab=departments` (and the designations and locations tabs) the **Add** button did nothing while the list was empty, so no organization record could ever be created and **Add employee** stayed blocked.

Cause: each list rendered its add/edit dialog inside the list body, and the shared list shell shows the body only when there is no empty state (`{empty ?? children}`). With no records the empty state replaced the body, so the button set its state but the dialog was never mounted. The existing test added a department to a list that already had one, so it never exercised the empty case. The API, permissions, audit and database were correct.

Fix: the shell takes the dialog as its own `dialog` prop and always renders it (`frontend/src/features/organization/OrganizationPage.tsx`); tests open each Add dialog from an empty list and show a field error from the API (`frontend/src/features/People.test.tsx`).

## 5. Dialogs that overflowed the viewport (2026-10-16)

Symptom: tall dialogs such as Add shift and Add leave type were cut off at the top or bottom of the window.

Cause: the shared `Dialog` centred its panel with flexbox inside a scrolling overlay. When the panel is taller than the window, flex centring pushes its top above the overlay's scroll origin, where it can never be scrolled to; the overlay also used the layout viewport, not the visual one, so zoom and on-screen keyboards made it worse.

Fix (shared, `frontend/src/design-system/Dialog.tsx`): the overlay takes React Aria's `--visual-viewport-height`; the panel is capped at that height; the title stays fixed and only the body scrolls; buttons sit in `DialogActions`, pinned to the bottom of the body. All 30 dialog button rows across the application now use it, and `ConfirmDialog` uses the same layout. Tests: `src/design-system/Dialog.test.tsx` (structure, focus moves in, Tab stays in, Escape closes, focus returns, axe) and `e2e/dialogs.e2e.ts` (seven add dialogs at five screen sizes in Chromium; each fails with the old layout).

## 6. Attendance and location

Built (revisions 0015 and 0020):

- Signing in and clocking in are separate. Sign-ins are sessions and login history (`/me/login-history`); attendance is the `CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT` events, recorded only when the person presses the button. Signing in never records an event.
- Each event stores server time (never the client's), the work date in the location's time zone, the source (`web`, `mobile`, `correction`, `import`), who recorded it, the client-reported time as metadata only, the IP address and the user agent. Events are append-only; corrections add events or voids.

Not built (roadmap Phase 2 item 12, ADR on attendance: "Geo/IP policies Phase 2"):

- No geolocation is requested, stored or evaluated. There is no geofence, no IP allow-list per location and no office, outside or remote status. Because nothing asks for location, there is no permission-denied path either. The `Permissions-Policy` header already limits geolocation to the HRMS origin.
- No work-from-home or on-duty request type.

Proposed design, for approval before any work starts:

1. Policy, not code: per location, an effective-dated attendance location policy with a mode (`off`, `record_only`, `warn`, `enforce`), office IP ranges and an optional geofence (centre and radius in metres), configured by HR admins under a new permission. No Sigvitas value is assumed; the default is `off`.
2. Capture once, at the moment of the event only: the browser asks for one position when the person presses Clock in or Clock out, never in the background and never on sign-in. No continuous tracking, no tracking outside these presses.
3. Store the result, not the trail: the computed classification (`office_network`, `inside_geofence`, `outside_geofence`, `location_unavailable`, `permission_denied`) and the reported accuracy, on the event. Raw coordinates are kept only if Sigvitas decides they are needed, encrypted, as restricted data with a retention period.
4. Wording: the product says "reported location", never "present". A browser position can be wrong or spoofed; it is evidence for review, not proof of presence. An IP address in an office range shows the network used, not the person.
5. Permission refused or unavailable: under `record_only` and `warn` the event is still recorded with that classification; under `enforce` the employee is told why it was refused and how to request a correction. Nobody is blocked from clocking in by a browser setting without HR having chosen `enforce`.
6. Audit and access: classifications are visible to the employee, their manager (team scope) and HR; a new `attendance.location.read` permission covers any stored coordinates, and each read of them is audited.
7. Privacy: a notice shown before the first request, covered by the DPDP obligations still to be confirmed (roadmap §6).

Inputs needed from Sigvitas: whether location controls are wanted at all; which locations; mode per location; office IP ranges; geofence radius; whether raw coordinates may be kept and for how long; whether remote work is allowed and how it is requested.

## 7. Workweek and leave policy configuration

### 7.1 Monday to Friday with an announced working Saturday

What exists (revisions 0020 and 0027):

- A shift version holds the start and end times and the weekly offs: any weekday, optionally only in some weeks of the month (for example the 2nd and 4th Saturday). Versions are effective-dated, so a change never rewrites past days. A location has a default shift; an employee can have their own assignment.
- "Saturday normally off" is a shift with Saturday (and Sunday, if so) as weekly offs. Nothing about it is built in: HR enters it.
- An announced working Saturday is a **declared working day** in the holiday calendar of the locations concerned (Holiday calendars → the calendar → Add a date → "Working day on a usual day off"). For that date only, attendance judges the day as a working day and leave requests count it; every other Saturday stays a weekly off and the shift is not touched. Adding or removing it is audited (`holiday.added`, `holiday.removed`, with `kind`).
- Scope: a declared working day applies to everyone at the calendar's locations. A working Saturday for only some teams at a location is not supported; the alternative today is a separate shift for those people.
- Not decided and not entered: shift times, grace, thresholds, overtime and every other policy value (roadmap §6). The engine applies no default: without a shift and policy a day is not judged.

Inputs needed from Sigvitas: the standard shift times; whether Saturday is off every week or only some weeks; whether Sunday is off; which locations follow it; who announces working Saturdays and how far ahead; whether a working Saturday ever applies to only some teams; the attendance policy values in `product-requirements.md` §6.12.

### 7.2 Leave policies

What exists and is configurable by HR admins, every change audited (`leave.type.created` / `updated`, `leave.policy.created`, `leave.accrual.run`, `leave.allocation.run`, `leave.balance.adjusted`, `leave.opening_balances.imported`, `leave.year_end.applied`):

- Leave types: paid or unpaid, needs approval, half days, whether holidays and weekly offs count.
- Policy versions per leave type, effective-dated, optionally for one location and/or one employment type (the most specific wins): leave year start; accrual none, upfront per leave year, or periodic (monthly or quarterly, at the start or end of the period) with an amount; proration on joining and exit with a rounding rule; negative balance and its limit; carry-forward none, capped or unlimited, with lapse or keep for the excess and an expiry in months for carried days.
- The ledger (append-only), manual adjustments and opening balances with reasons, the all-or-nothing opening-balance import, the year end with a preview, approvals by the reporting manager or HR.
- Not built: encashment, attachments, multi-level approval, eligibility by anything other than location and employment type.

The proposal "one day a month for men, two for women" is **not configured and cannot be configured**, by design:

- Employee records hold no gender, and nothing may infer it from a name or email. `database-design.md` keeps a `gender` field for when Sigvitas asks for it; it is not built.
- Policies select people by location and employment type only. A different entitlement for a group of people would need an explicit, audited eligibility rule that HR assigns, never a derived attribute.
- Whether different entitlements by gender are lawful for Sigvitas, and on what basis, is a legal question this project cannot answer. No code assumes an answer.

If Sigvitas decides to differentiate entitlements after legal review, the smallest safe design is **eligibility groups**: named groups HR creates; membership assigned per employee by HR with a reason, effective-dated and audited; a policy version may be limited to a group, like a location today. Membership is a restricted data tier if it reveals a protected characteristic. Nothing is built until the decision is made.

Decisions needed from Sigvitas HR (with legal review where marked):

| Decision | Notes |
|---|---|
| Leave types and which are paid | |
| Who is eligible for each type (all, by location, by employment type, by a group) | Legal review if any group reflects a protected characteristic |
| Entitlement per type: upfront per year or monthly accrual, and the amount | One day a month is `periodic`, monthly, amount 1 |
| Accrual timing (start or end of month) and proration on joining and exit, with rounding | |
| Leave year start | |
| Whether holidays and weekly offs inside a request count | Per type |
| Negative balance and its limit | |
| Carry-forward (none, capped, unlimited), lapse or keep of the excess, expiry of carried days | |
| Encashment: whether, which types, when, how many days | Not built; affects payroll (§8) |
| Opening balances at go-live and their source | |
| Approvers when there is no reporting manager; whether HR may decide team leave | |
| Attachments (for example medical) and retention | Documents exist; leave attachments not built |

## 8. Compensation and payroll

Current state, checked in the code on 2026-10-16:

- No compensation, payroll or payslip table, endpoint, page or permission exists. The permission catalog lists MVP permissions only.
- The `payroll_admin` role exists and today holds only read access to employee profiles, attendance and leave, plus attendance and leave exports, so payroll can be prepared outside the system from those exports.
- Attendance locks (ATT-11) exist and can be aligned with payroll periods.
- `super_admin` can never hold salary access directly (CLAUDE.md rule 16).

Requirements already decided (`product-requirements.md` §6.6): effective-dated compensation (PAY-1), configurable components (PAY-2), periods `draft → in_review → finalized → published` (PAY-3), four-eyes finalization (PAY-4), payslip snapshots (PAY-5), PDF payslips through 60-second signed links (PAY-6), audit of every read of another person's compensation (PAY-7). No statutory calculation (PF, ESI, PT, TDS or any other) is to be built until Sigvitas asks for it.

Unresolved, needed from Sigvitas before the matching phase:

| Question | Blocks |
|---|---|
| In-house calculation or an external payroll provider | Everything after phase P1 |
| Pay frequency, pay day and period boundaries | P2 |
| Components used (names, earning or deduction, fixed or percentage of another) | P2 |
| Whether four-eyes finalization stays on | P3 |
| Bank details needed and who may see them | P2 |
| Statutory schemes that apply, if any, and who confirms the rules | Future only |
| Retention period for payroll records and payslips | P3 |
| Payslip layout and legal text | P3 |

Phased plan (each phase needs the owner's approval and a new checkpoint; none has started):

- **P1 — payslip publishing only.** If an external provider is used: payroll admins upload payslip PDFs per period, scanned and stored like documents, published to employees through audited 60-second links. Permissions `payslip.publish`, `payslip.read.self`, `payslip.read.all`. No salary figure is stored.
- **P2 — compensation records.** Effective-dated compensation with components and money as `numeric(14,2)` plus currency; `compensation.read.*` and `compensation.manage` permissions held by `payroll_admin` only; every read of another person's record audited (PAY-7); passkeys required for `payroll_admin` (Phase 2 item 2).
- **P3 — payroll periods.** Period state machine, attendance and leave inputs taken from locked periods, gross and net from the configured components only, four-eyes finalization, payslip snapshots and PDFs, adjustments in later periods instead of edits.
- **Future — statutory rules**, only on Sigvitas's request, as effective-dated configuration with values confirmed by Sigvitas's adviser, never constants in code.

## 9. Supported record actions

Records are not deleted wholesale. Each kind of record has one way to stop being used, chosen by what refers to it; the API enforces the permission and every dependency check, and the web app explains the consequence before the person confirms. Hard deletion happens only for configuration rows with no history of their own (a calendar entry, a location's calendar assignment); a person's own notifications are soft-deleted. Every one is audited with the values removed.

| Record | Action | Rule enforced by the API | Permission |
|---|---|---|---|
| Location, department, designation | Archive, restore (edit dialog) | Refused while an employee who has not left is placed in it, now or later; a department also while active departments sit inside it. Job history keeps it. | `org.manage` |
| Employee | Status change; offboarding (the one-step exit: **Offboard employee** on the record and its Status tab, details then a confirmation naming the person, then what was done) | Never deleted; status history and job history are append-only. Offboarding disables the linked account, ends its sessions, forgets its trusted browsers and cancels an open invite; an account linked to someone who has left cannot be enabled again (`409 user.employee_exited`). Permanent deletion or anonymization is not built: it waits for the retention periods Sigvitas legal confirms (`database-design.md` §9). | `employee.lifecycle.manage` (+ `user.disable` and step-up when an account is linked) |
| User account | Disable, enable; revoke an invite; end sessions | Disable needs step-up and ends the account's sessions; administrator accounts only by a `system_admin`. | `user.disable`, `user.invite`, `auth.session.revoke.all` |
| Role assignment | Revoke | Rows are revoked, never deleted (trigger). | `role.assign` |
| Shift | Archive, restore (edit dialog) | Refused while anyone is assigned to it or a location uses it as its default, now or later. Its versions stay, so past days are still judged by them. | `shift.manage` |
| Shift version, attendance policy, location default, shift assignment, leave policy | Superseded by a newer effective-dated row | Rows are never edited; only an open `effective_to` is set when a newer row takes over. | `shift.manage`, `attendance.policy.manage`, `shift.assign.all`, `leave.policy.manage` |
| Attendance event | Correction (request and approval, or HR-recorded) | Events are never updated or deleted; a correction voids and adds events; locked dates refuse corrections. | correction permissions |
| Attendance lock | Release with a reason | Never deleted. | `attendance.policy.manage` |
| Leave type | Archive, restore (edit dialog) | Refused while a request of the type waits for a decision (`409 leave.type_in_use`, 2026-10-16). Archived types take no new requests; balances, ledger and past requests stay. | `leave.policy.manage` |
| Leave ledger entry | Adjustment with a reason | Append-only (trigger); never on one's own balance. | `leave.balance.adjust` |
| Leave request | Cancel (own), reject, decide a cancellation | A pending request is withdrawn; approved leave that has not started is cancelled or goes to the approver; usage is reversed by a new ledger entry. | `leave.request.self`, `leave.request.approve` |
| Holiday calendar | Rename, archive, restore | Archive refused while any location uses the calendar (`409 holiday.calendar_in_use`); an archived calendar refuses every change except restoring it and removing a location (`409 holiday.calendar_archived`). | `holiday.manage` |
| Holiday or declared working day | Edit the name and the optional choice; remove | The date and kind never change in place (remove and add again); a working day is never optional. | `holiday.manage` |
| Location's calendar | Assign, replace, remove | One calendar per location and year. | `holiday.manage` |
| Document category | Archive, restore (edit dialog) | Archived categories take no new documents; documents in them keep their access rules. Sensitivity cannot change once documents use the category. | `document.category.manage` |
| Document | Archive, restore; new version | Never deleted by the application; archived documents take no new versions. | document permissions of the category |
| Emergency contact | Edit the list, remove a contact | The list is replaced as a whole and audited. | personal-tier permissions |
| Notification | Delete (own) | Soft delete (`deleted_at`). | own only |
| Export file | None | Expires 24 hours after it is produced; the worker deletes the file. | |
| Audit row | None | Never updated or deleted; partitions are removed only by the retention job after a tombstone. | |

Tests: `backend/tests/integration/test_record_actions.py` (entry edit, calendar archive guards, leave-type archive guard, audit rows, `403` for HR, `404` across calendars, `412` on a stale version, `422` for date or kind changes); the existing archive guards in the organization, shift and document tests; frontend tests in `AttendanceAndAdmin.test.tsx` and `Leave.test.tsx`.

Gaps, not built: ending a personal shift assignment so the person falls back to the location default; ending a leave or attendance policy without a successor; HR cancelling leave that has already started. Holiday entry changes are not checked against attendance locks, and because day status is derived when read, removing a past holiday or working day changes past day status (the confirmation says so).

## 10. Theme and fonts

- Light, dark and "Same as system", chosen in the account menu (Theme section, operable by keyboard, `menuitemradio` items). The choice is stored in this browser only (`localStorage`, key `sv-theme`); it is a display preference and never reaches the API.
- `public/theme-init.js` runs before the app (a same-origin script, allowed by `script-src 'self'`) and sets `data-theme` on `<html>`, so there is no flash of the wrong theme. The dark theme redefines the same colour tokens in `src/styles.css`; components have no dark-only classes. Dialog and drawer backdrops use the `scrim` token.
- A theme change fades colours for 200 ms; with reduced motion it is instant. No 3D or WebGL.
- Fonts: IBM Plex Sans (400, 600) for the interface and IBM Plex Mono (400) for codes and IDs, self-hosted from `@fontsource` packages with `font-display: swap`. Before 2026-10-16 codes fell back to the system monospace font because Plex Mono was not installed.
- Verified in Chromium (`e2e/theme.e2e.ts`): the stored theme is set while the app's code is held back; the system preference, persistence across a reload, reduced motion; axe WCAG 2.2 AA (colour contrast included) on six screens in both themes and on the open account menu; both fonts loaded. The checks were shown to fail when the startup script was removed or a dark token was made low-contrast.
- Form field borders were below 3:1 in both themes (`neutral-300`: 1.56:1 light, 1.92:1 dark). Fixed on 2026-10-19 with the `control-border` token (§13). Checkbox borders were raised to `neutral-500` on 2026-10-16. Secondary-button and view-switch borders stay `neutral-300` (measured 1.56:1 light, 1.92:1 dark; the view-switch group edge 1.49:1 and 2.08:1 against the page); their text labels identify them, so SC 1.4.11 does not require the edge (§13).

## 11. Checkboxes that never looked checked (2026-10-16)

Symptom: on Notifications, What I receive, clicking a checkbox showed "Preference saved", but the box still looked empty; quick clicks stacked several identical toasts.

Cause: the state was right (React Aria set `data-selected`, the API saved the value and returned it), but the shared `Checkbox` drew its fill and tick with `group-selected:` classes. Tailwind v4 has no `selected` variant (it comes from a React Aria plugin that is not installed), so those classes compiled to no CSS at all. The same gap hid the selected tab underline, the selected option in Select and ComboBox, and the primary button's pressed shade. jsdom tests passed because they check attributes, not styles. Also, the box changed only after the API answered, and the toast list kept duplicates.

Fix: `@custom-variant selected` and `pressed` in `src/styles.css`; the checkbox draws a keyboard focus ring and a disabled state on its visible box; preference changes show at once, the API's answer becomes the truth, a refused save puts the box back and shows the error (no success toast), and only the last answer applies when several are in flight; the toast list shows a repeated message once and restarts its time. Every checkbox in the application uses the shared component, so all are fixed.

Tests: `e2e/checkboxes.e2e.ts` in Chromium (immediate fill, value sent, reload restores, refused save, one toast for quick changes, keyboard with a visible ring, dark theme, 200 % zoom, selected tab underline and no tab-strip scrollbar; three fail with the variants removed), `src/design-system/Fields.test.tsx` (click, Space, disabled, toast grouping), preference tests in `Operations.test.tsx`, and the backend read-back test in `test_notifications_and_exit_api.py`.

Scrolling found with it: the Notifications tab strip had its own vertical scrollbar, from a 1 px overflow; table headers were meant to be sticky but were not. Both fixed at the cause (`ui-ux-guidelines.md` §4, Scrolling; `e2e/scrolling.e2e.ts`).

## 12. Data views (2026-10-16)

Researched first: Atlassian table tree and motion foundations, WCAG 2.2, MDN scrollbar styling. Built, using data the API already returns and no new calculation:

- **Attendance:** each day in the table opens to its clock events in time order, with events removed or added by a correction marked in words, the shift, and the API's figures. A Month view (React Aria Calendar: arrow keys, month buttons, later dates disabled) shows each day's status with a key; choosing a date opens the same details. The table stays the default.
- **Audit log:** each record opens to its field changes, reason and full request context (request, session, address, browser, full IDs). Refused and failed actions say so in words.
- **Organization:** Departments has a Hierarchy view (table tree) beside the list: children render only when expanded, Expand all and Collapse all, a selected-department panel with its parent, sub-departments, headcount (when the reader may see headcounts) and a link to its people (only for readers of the employee list, which applies their scope).
- **Leave calendar:** a new month fades in.

Verified in Chromium (`e2e/data-views.e2e.ts`): event order and correction marks, month navigation by button and arrow keys, the audit details, lazy hierarchy children, axe in both themes, no sideways scrolling at 200 % zoom, reduced motion. That test found and fixed a keyboard defect (arrow keys started from the 1st of the month) and grey text below 4.5:1 on expanded rows.

Not built, and why: manager and employee levels in the hierarchy (teams are not a stored concept, and listing people per node needs a scoped people endpoint per department, not yet designed); a team or HR attendance heatmap (needs a bounded multi-employee days endpoint); charts in reports (the report endpoints return tables, and no chart library is in the stack; adding one needs a stated reason); virtualization (not needed while children render only on expansion and lists page by 50).


## 13. Final verification and field contrast (2026-10-19)

Field contrast: text field, text area, select, combo box and search field edges were `neutral-300` (1.56:1 light, 1.92:1 dark, below WCAG 1.4.11's 3:1). They now use the `control-border` token (#8a847c light, #7d7670 dark), at least 3.28:1 against `neutral-0`, `neutral-50` and `neutral-100` in both themes. Colours, radii, sizes and IBM Plex fonts are otherwise unchanged. Found with it: the text field's red invalid edge never showed, because it used the native `:invalid` state while forms validate with `validationBehavior="aria"`; every field now draws it from `data-invalid` (the Select from its root). File inputs keep the browser's own control.

Authorization, checked in code and tests: the department hierarchy shows only what `GET /departments` and `GET /employees/statistics` return (headcounts are scoped by `employee.profile.read`); its "People in" link opens `GET /employees?department_id=…`, where the filter narrows the reader's scope and never widens it (new test `test_the_department_link_lists_only_the_manager_s_team`). Audit details render only fields already in the `GET /audit-log` response, which requires `audit.read` and records each read; the details make no further request.

Tests added: `e2e/field-contrast.e2e.ts` (the token and the rendered fields on four screens and dialogs, both themes; shown to fail with the old colours), a keyboard and focus-outline test for audit details and the hierarchy in `e2e/data-views.e2e.ts`, and the invalid-marker and field-token test in `src/design-system/Fields.test.tsx`.

Results on 2026-10-19: frontend format, lint, strict types, copy check and build pass; Vitest 144 of 144; Playwright 91 of 91 in Chromium (checkboxes, scrolling, dialogs, theme, data views, field contrast); backend ruff, mypy, import contracts and pytest 797 of 797. A Vitest run made while the backend suite loaded the machine had 7 tests miss the 5 s wait for a lazy page; an idle run passed all of them.

Pre-commit review, same day: secondary-button and view-switch edges were measured in both themes (see §10) and need no change. The view switch's chosen state did fail: its fill and text colour differed from the other button by 1.11:1 and 1.03:1 (light) and 1.13:1 and 1.09:1 (dark), with the same weight, so the choice was shown by a faint colour change only (SC 1.4.11 for states, SC 1.4.1). The chosen button now also has a 2 px `accent-600` bar along its bottom edge, as the selected tab does (at least 3:1 against its fill in both themes); a test in `e2e/field-contrast.e2e.ts` checks it in both themes and was shown to fail without the bar. File pickers (document upload and new version, leave opening-balance CSV) are native inputs inside their labels: reachable by Tab, opened with Enter or Space, named by the label, with the standard focus outline; their look is the browser's, which SC 1.4.11 exempts. The document picker's error was not linked to the input; it now is (`aria-describedby`, `aria-invalid`; test in `Operations.test.tsx`). The CSV import's error is an announced banner.

Pre-commit runs, same day (all fresh): backend ruff, mypy, import contracts, pytest 797 passed, 0 skipped; pip-audit, pnpm audit, gitleaks (history and the uncommitted diff), Semgrep: no findings; the OpenAPI contract matches. Frontend format, lint, types, copy check and build pass. Vitest in parallel: 141 to 142 of 145 per run, and 145 of 145 with `--maxWorkers=4` and per file; the failures move between runs and each is a first lazy page missing the 5 s wait, and an unmodified copy of the previous commit fails the same way (3 of 143). Playwright: 92 of 93; the one failure moved between two checkbox tests across runs and each passes six times in a row alone. One was a colour read during the box's 120 ms transition, now polled; the other (`survives a reload`) polls the fill for 250 ms on purpose and is left as it is.

Intermittent failures, resolved during deployment preparation (same day). Neither was a product defect:
- Vitest: `App.tsx` lazy-loads 42 pages, so each file's first render compiles its page; with one worker per core (13 files at once on a 12-thread machine) that import sometimes took longer than the 5 s wait in `src/test/setup.ts`. `vite.config.ts` now runs 4 workers; three full runs in a row passed 145 of 145. No wait or assertion changed.
- Playwright `survives a reload`: it proved "filled before the API answers" by a 250 ms deadline, which a busy machine can miss. The fake API now holds its answer until the test has seen the box checked and filled and has confirmed the save is still unanswered, then releases it: the same claim, checked exactly instead of by time (a box that waited for the answer would now fail outright). 35 of 35 checkbox runs passed.

Not covered: browsers other than Chromium; screen reader runs by a person.
