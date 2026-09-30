# Sigvitas HRMS — UI/UX Guidelines and Design System Direction

Status: Draft v0.1. Direction only; tokens and components are built in Phase 1.

## 1. What this product should feel like

An internal tool people open several times a day to do a specific thing — clock in, check a balance, approve a request — and then leave. Success is measured by how quickly and confidently they finish, not by how long they stay.

The interface should read as calm, plain and deliberate: clear type, generous but not wasteful spacing, one accent colour used sparingly, and information arranged by what the person needs to do next.

### Avoid

- Gradients, glassmorphism, floating blobs, glow effects, decorative 3D.
- Dashboards made of equal-sized cards with big numbers that nobody acts on.
- Charts where a sentence or a table would be clearer.
- Illustrations of people, stock photos, AI-generated imagery.
- Marketing tone inside the app ("Welcome to your journey!").
- Animated counters, confetti, parallax.

## 2. Information architecture

Navigation is grouped by *whose* data the person is working with, and sections appear only if the user has permissions for something in them.

| Group | Items (MVP) | Who sees it |
|---|---|---|
| **Me** | Today, Attendance, Leave, Documents, Profile | Everyone |
| **Team** | Approvals, Team attendance, Team leave calendar, Team members | Managers, HR |
| **People** | Employees, Organization (departments, designations, locations) | HR, HR admin |
| **Time and leave setup** | Setup status, Shifts, Attendance policies, Leave types and policies, Holiday calendars, Attendance locks | HR admin |
| **Reports** | Attendance, Leave, Exports | Those with report data access |
| **Administration** | Users, Roles, Access requests (elevations, break-glass reviews), Security events, Audit log, Settings | System admin, super admin, auditor (each sees only the items their permissions allow) |

Phase 2 adds Payroll (payroll admins), Pay (employees: payslips, compensation), Requests, Assets, Announcements.

### Home ("Today")

Not a metrics dashboard. It answers "what do I need to do now?":

1. Attendance state with the single primary action (Clock in / Start break / End break / Clock out) and today's running time.
2. Items waiting for the user (approvals for managers; returned corrections for employees).
3. Upcoming: approved leave, next holiday, team members away today (managers).

Anything else lives on its own page.

## 3. Layout

- **Desktop (≥ 1024 px):** left sidebar (240 px, collapsible to icons), top bar with global search (directory in MVP), notifications and account menu. Content max width 1280 px for tables, 720 px for forms and reading.
- **Tablet (640–1023 px):** sidebar collapses to an overlay drawer; top bar remains.
- **Mobile (< 640 px):** bottom navigation with four destinations — Today, Attendance, Leave, More — plus Approvals replacing Attendance for managers with pending items (decided in Phase 1 usability check). "More" opens a full-screen list of remaining sections. Primary actions are reachable by thumb; the clock action on Today is a full-width button.
- Page structure: page title (h1), optional one-line description, primary action on the right (desktop) or bottom (mobile), then filters, then content.
- Avoid nested cards. Use sections separated by headings and whitespace; use a bordered surface only for grouped, related content (e.g. a day's timeline).

## 4. Visual foundation

### Typography

- One family for UI: **IBM Plex Sans** (open licence, clear at small sizes, true tabular figures, good Latin and Devanagari companions if Indian-language UI is ever needed). **IBM Plex Mono** for codes and IDs where monospace helps (employee codes in tables). Self-hosted `woff2`, subset, `font-display: swap`.
- Type scale (rem at 16 px base): 0.75 (caption), 0.875 (secondary/table), 1 (body), 1.125 (section title), 1.375 (page title), 1.75 (rare, large numeric displays such as today's worked time).
- Weights: 400 and 600 only (500 optional for table headers).
- Numbers in tables and time displays use `font-variant-numeric: tabular-nums`.
- Line length for reading text: 60–75 characters.

### Colour

Sigvitas brand colours are not yet provided (open question). Until then, the direction is:

- Neutral scale slightly warm (not blue-grey), 11 steps, used for 90% of the interface.
- One accent (proposed: a deep teal or ink blue, final value from Sigvitas brand) for primary buttons, links, focus rings and the current nav item. Nothing else.
- Semantic colours with meaning only: success (approved, present), warning (pending, late, incomplete), danger (rejected, absent, destructive actions), info (neutral status). Each paired with an icon or text; colour is never the only signal.
- All text/background pairs meet WCAG 2.2 AA contrast (4.5:1 body, 3:1 large text and UI components).
- Light theme first; dark theme defined in tokens from day one and shipped when verified.

### Spacing, shape, elevation

- 4 px base grid; common steps 4, 8, 12, 16, 24, 32, 48.
- Radius: 6 px for controls, 8 px for surfaces, full for avatars and status pills.
- Elevation used only for things that float (menus, dialogs, toasts). Page content is flat with 1 px borders.

### Icons

- **Lucide** (ISC licence), 16/20 px, 1.5 px stroke, tree-shaken per icon. Icons accompany labels; icon-only buttons have an accessible name and a tooltip.
- No emoji in headings, labels or status indicators.

### Tokens

Defined as CSS custom properties consumed by Tailwind v4's `@theme`: `--color-*`, `--space-*`, `--radius-*`, `--font-*`, `--shadow-*`, `--duration-*`, `--ease-*`. Components use tokens only; no raw hex values in components (lint rule).

## 5. Components

Built once in `frontend/src/design-system/`, on **React Aria Components** for behaviour and accessibility, styled with Tailwind. No second component library.

MVP set: Button, IconButton, Link, TextField, TextArea, NumberField, Select, ComboBox (employee picker), Checkbox, RadioGroup, Switch, DatePicker, DateRangePicker, TimeField, Dialog, AlertDialog (confirmation), Drawer (mobile sheets), Menu, Tabs, Tooltip, Toast, Badge/StatusPill, Avatar, Table (TanStack Table + React Aria semantics), Pagination (cursor "Load more" / next-prev), EmptyState, ErrorState, Skeleton, PageHeader, FilterBar, DescriptionList (label/value pairs), Timeline (attendance day view), Calendar (leave calendar).

Rule: if a pattern appears twice, it becomes a component; if it appears once, it stays in the feature.

## 6. Patterns

### Tables

- Default density "comfortable" (44 px rows); "compact" toggle (36 px) remembered per user.
- Sticky header, left-aligned text, right-aligned numbers, status as pill + text.
- Row click opens detail; row actions in a trailing menu; bulk actions only where the backend supports them.
- On mobile, tables become stacked lists: primary field as title, two or three secondary fields, status pill. Not horizontal scrolling for primary tables.
- Filters shown above the table as a compact bar; active filters are visible chips with clear-all. Filter state is kept in the URL so views can be bookmarked and shared (without leaking data, since the receiver's permissions apply).

### Forms

- Labels above fields; helper text below the label when needed; required fields marked "Required" in text only when most fields are optional, otherwise mark "Optional".
- Validate on blur and on submit, not on every keystroke. On submit with errors: focus moves to an error summary at the top linking to each field.
- Server errors map to fields by `field` in problem details.
- Primary button text says what happens: "Submit leave request", "Approve", "Save changes". Never "OK" or "Submit" alone.
- Unsaved changes prompt on navigation for long forms.
- Leave request form shows a live preview from `/me/leave-requests/preview`: counted days, excluded holidays/weekly offs, resulting balance.

### Approvals

- One queue per type with the most important context inline (who, what dates, balance after approval, team members already away on those dates).
- Approve is one click; Reject requires a note.
- Attendance correction review shows original and proposed timelines side by side, with voided events struck through (not hidden).

### Confirmations

- Reversible actions: no confirmation; show a toast with "Undo" only where the backend supports undo (e.g. cancel a pending request).
- Consequential actions (reject, deactivate user, revoke sessions): AlertDialog naming the object and consequence ("Deactivate {employee name}'s account? They will be signed out on all devices.").
- High-impact administrative actions (exit employee with immediate revocation, assign super admin, finalize payroll): AlertDialog + step-up. No typed-name confirmation unless the action is irreversible and bulk.

### Account activation and MFA enrolment

Every account enrols MFA during activation. It is part of the sign-up flow, not an optional setting:

1. "Set your password" (strength feedback, breached-password message in plain words).
2. "Set up your authenticator app" — QR code plus a manual key, a short list of common authenticator apps without endorsing one, then "Enter the 6-digit code to confirm".
3. "Save your recovery codes" — shown once, with download and copy actions, and a required checkbox "I have saved these codes" before continuing.

The flow works on a phone alone: the manual key can be copied into an authenticator app on the same device. Each step states what happens next. There is no "Skip" button.

The same screens serve re-enrolment after a recovery-code sign-in or an admin MFA reset, with a banner explaining why.

### Step-up

A small dialog: "Confirm it's you — Enter the 6-digit code from your authenticator app." After success the original action continues automatically. The dialog never offers a password fallback.

### Access requests (super admin)

- The request form asks for the role, reason (required), duration (default 2 hours, maximum 8) and optional scope. The consequence is stated plainly: "Other super admins will be notified. Access ends automatically at {time}."
- While elevated, a persistent, non-dismissible banner shows the role and remaining time, with an "End now" action.
- Break-glass is a separate, visually distinct action with an explanation that only account-recovery access is granted, for one hour, and that all super admins are alerted.

### Policy configuration (HR admin)

- Policies are shown as plain-language summaries generated from the values ("Late after 10 minutes past shift start"), next to the editable fields.
- Every change asks for an effective date and shows a preview of the impact on recorded days before saving ("This changes the status of 14 days for 6 employees from 1 Oct").
- Setup gaps (a location without a shift or policy) are listed on "Setup status" with a direct link to fix each one. Nothing is computed with hidden defaults.

### States

- **Loading:** skeletons shaped like the content for first load; inline spinners only inside buttons. Nothing for loads under 300 ms (delay the skeleton).
- **Empty:** one sentence stating what would appear here and, if the user can act, the action. Example: "No leave requests yet. Requests you submit will appear here." No illustrations.
- **Error:** what failed, what the person can do, and the request ID in small text for support. Example: "We couldn't load attendance for September. Try again. If this keeps happening, contact HR with reference 01J…"
- **No permission:** pages the user cannot access are not linked. If reached by URL: "You don't have access to this page."
- **Offline (mobile):** banner "You're offline. Clocking in needs a connection." Clock actions are never queued offline in MVP (server time is authoritative).

### Sensitive data display

- Masked by default: `XXXX XXXX 1234`. "Show" requires step-up and re-masks after 60 seconds or on navigation.
- Salary figures (Phase 2) hidden behind a "Show amounts" control on shared screens.
- No sensitive data in page titles (browser history) or URLs.

## 7. Motion

- Purpose only: indicate a change of state (toast in, dialog open, row inserted), preserve spatial context (drawer slides from its edge), or give feedback (button press).
- Durations 120–200 ms; standard easing `cubic-bezier(0.2, 0, 0, 1)`. Nothing over 300 ms except page-level skeleton fades.
- `prefers-reduced-motion: reduce` → transitions become instant opacity changes; no transforms.
- **Motion** (the library formerly Framer Motion) only for layout/presence animations that CSS transitions cannot handle (list reorder, exit animations). CSS transitions first.
- 3D: none in MVP. If introduced, limited to a static, lightweight brand element on the sign-in page, never in working screens.

## 8. Accessibility (WCAG 2.2 AA)

- Semantic HTML first: `nav`, `main`, `header`, headings in order, `table` for tabular data, `button` for actions, `a` for navigation.
- Skip link to main content. Landmarks on every page.
- Visible focus ring (2 px accent, 2 px offset) on every interactive element; never removed.
- Full keyboard operation including tables, date pickers, menus and dialogs (React Aria provides the patterns). Focus returns to the trigger when dialogs close.
- Minimum target size 24×24 px (WCAG 2.5.8); 44×44 px for primary mobile actions.
- Status changes announced via live regions (clock-in success, form errors).
- Forms: every input labelled; errors linked with `aria-describedby`.
- Zoom to 200% without loss; reflow at 320 px.
- Testing: axe checks in component and end-to-end tests; manual keyboard and screen reader pass (NVDA + Firefox, VoiceOver + Safari) on each major flow before release.

## 9. Writing

### Voice

Plain, direct, polite. Write as a helpful colleague in HR would speak. Use "you" for the reader. Short sentences.

### Rules

- Sentence case for all headings, buttons and labels ("Leave requests", not "Leave Requests").
- Name things by what Sigvitas calls them. Confirm terms with HR (e.g. "regularization" vs "attendance correction"; the UI uses "Attendance correction" unless Sigvitas prefers otherwise).
- Buttons are verbs. Status labels are adjectives or past participles ("Approved", "Pending").
- Dates: "Tue, 30 Sep 2026" in lists; relative ("2 hours ago") only for notifications and activity, with the absolute time on hover.
- Durations: "7h 42m".
- Numbers: locale formatting (en-IN proposed: 1,25,000.00 for currency).
- No exclamation marks except in rare genuine congratulations (none planned).

### Banned words and patterns

seamless, cutting-edge, revolutionize, empower, skyrocket, next-generation, game-changing, delve, leverage, transformative, unlock, supercharge, effortless, magic, journey (for employment), "Oops!", "Uh oh", "Something went wrong" without further detail.

A CI check greps UI copy files for the banned list.

### Content integrity

- No invented employees, departments, statistics, testimonials, policies or company claims in the product.
- Development seed data uses obviously fake values (see `database-design.md` §10).
- Help text describing policies (leave rules, attendance rules) is rendered from Sigvitas's configured policies, not hard-coded prose.

## 10. Performance budget (web)

- Initial JS for the authenticated shell ≤ 200 KB gzipped; each feature route lazy-loaded.
- Largest Contentful Paint < 2.5 s on a mid-range Android device over 4G for Today.
- Interaction to Next Paint < 200 ms.
- No blocking web fonts; fonts preloaded and subset.
