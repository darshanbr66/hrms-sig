import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import type { components } from "../lib/api-schema";
import { FakeApi, json, problem } from "../test/fakeApi";
import { EMPLOYEE_ME, HR_ADMIN_ME, MANAGER_ME, attendanceDay, employee, today } from "../test/fixtures";
import { renderSignedIn } from "../test/render";

let api: FakeApi;

beforeEach(() => {
  api = new FakeApi();
  api.install();
});

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "geolocation");
});

type Outcome = components["schemas"]["EventLocationView"];

/** A browser whose geolocation answers as given (or never, for `pending`). */
function browser(answer: "granted" | 1 | 2 | 3 | "pending" | "none", accuracy = 12) {
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  if (answer === "none") return vi.fn();
  const getCurrentPosition = vi.fn((success: PositionCallback, failure: PositionErrorCallback) => {
    if (answer === "pending") return;
    if (answer === "granted") {
      success({
        coords: { latitude: 12.0001, longitude: 77.0002, accuracy } as GeolocationCoordinates,
        timestamp: Date.now() - 2000,
      } as GeolocationPosition);
      return;
    }
    failure({
      code: answer,
      PERMISSION_DENIED: 1,
      POSITION_UNAVAILABLE: 2,
      TIMEOUT: 3,
    } as GeolocationPositionError);
  });
  Object.defineProperty(navigator, "geolocation", { value: { getCurrentPosition }, configurable: true });
  return getCurrentPosition;
}

function clockData(check: "off" | "requested", outcome: Outcome | null) {
  const clockedIn = today({
    state: "clocked_in",
    allowed_events: ["BREAK_START", "CLOCK_OUT"],
    location_check: check,
    day: attendanceDay({
      open_session: true,
      events: [
        {
          id: "ev-1",
          event_type: "CLOCK_IN",
          occurred_at: "2026-10-05T03:30:00Z",
          source: "web",
          voided: false,
          correction_id: null,
          location: outcome,
        },
      ],
    }),
  });
  api
    .on("GET /api/v1/me/profile", json(employee()))
    .on("GET /api/v1/me/attendance/today", json(today({ location_check: check })))
    .on("GET /api/v1/me/holidays", json({ items: [] }))
    .on("GET /api/v1/me/login-history", json({ items: [], next_cursor: null }))
    .on("POST /api/v1/me/attendance/events", json(clockedIn))
    .on("GET /api/v1/me/attendance/today", json(clockedIn));
}

async function pressClockIn() {
  const user = userEvent.setup();
  renderSignedIn(api, EMPLOYEE_ME, "/dashboard");
  await user.click(await screen.findByRole("button", { name: "Clock in" }));
  return user;
}

function sentLocation() {
  return (api.called("POST /api/v1/me/attendance/events")[0]?.body as { location?: Record<string, unknown> })
    .location;
}

describe("clocking with a location check", () => {
  it("asks nothing when no office check applies", async () => {
    const asked = browser("granted");
    clockData("off", null);
    await pressClockIn();
    await screen.findByRole("button", { name: "Start break" });
    expect(asked).not.toHaveBeenCalled();
    expect(sentLocation()).toBeUndefined();
    expect(screen.queryByText(/About location checks/)).not.toBeInTheDocument();
  });

  it("sends one reported position with the press and says the result in plain words", async () => {
    const asked = browser("granted");
    clockData("requested", { outcome: "verified", reason_codes: [] });
    await pressClockIn();
    expect(
      await screen.findByText("Your reported location was within an approved office."),
    ).toBeInTheDocument();
    expect(asked).toHaveBeenCalledTimes(1);
    const location = sentLocation();
    expect(location).toMatchObject({
      status: "captured",
      latitude: 12.0001,
      longitude: 77.0002,
      accuracy_m: 12,
    });
    expect(typeof location?.captured_at).toBe("string");
    expect(typeof location?.sent_at).toBe("string");
    // Neither a distance nor an outcome is ever sent by the browser.
    expect(location).not.toHaveProperty("distance_m");
  });

  it.each([
    [1, "permission_denied", /Location access was blocked/],
    [2, "unavailable", /could not find its location/],
    [3, "timeout", /took too long/],
  ] as const)("records the press when the browser fails (%s)", async (code, status, message) => {
    browser(code);
    clockData("requested", { outcome: "unverified", reason_codes: [status] });
    await pressClockIn();
    expect((await screen.findAllByText(message)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Your attendance was recorded/).length).toBeGreaterThan(0);
    expect(sentLocation()).toEqual({ status });
    if (status === "permission_denied")
      expect(screen.getAllByText(/change this site's location permission/).length).toBeGreaterThan(0);
  });

  it("reports an unsupported browser without asking", async () => {
    Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
    clockData("requested", { outcome: "unverified", reason_codes: ["unsupported"] });
    await pressClockIn();
    expect((await screen.findAllByText(/browser cannot share its location/)).length).toBeGreaterThan(0);
    expect(sentLocation()).toEqual({ status: "unsupported" });
  });

  it("lets the person continue without location while the browser is still looking", async () => {
    browser("pending");
    clockData("requested", { outcome: "unverified", reason_codes: ["skipped"] });
    const user = await pressClockIn();
    expect((await screen.findAllByText(/Getting your reported location/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Clock in" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Continue without location" }));
    expect((await screen.findAllByText(/Continued without location/)).length).toBeGreaterThan(0);
    expect(sentLocation()).toEqual({ status: "skipped" });
  });

  it("explains a stale or inaccurate position as not a mismatch", async () => {
    browser("granted", 900);
    clockData("requested", { outcome: "inconclusive", reason_codes: ["low_accuracy"] });
    await pressClockIn();
    expect(
      (await screen.findAllByText(/not precise enough, so the check is inconclusive/)).length,
    ).toBeGreaterThan(0);
  });

  it("explains a stale position", async () => {
    browser("granted");
    clockData("requested", { outcome: "unverified", reason_codes: ["stale_position"] });
    await pressClockIn();
    expect(
      (await screen.findAllByText(/was too old, so your location was not checked/)).length,
    ).toBeGreaterThan(0);
  });

  it("shows the privacy notice before anything is asked", async () => {
    const asked = browser("granted");
    clockData("requested", null);
    const user = userEvent.setup();
    renderSignedIn(api, EMPLOYEE_ME, "/dashboard");
    await user.click(await screen.findByRole("button", { name: "About location checks" }));
    const notice = await screen.findByRole("dialog", { name: "About location checks" });
    expect(notice).toHaveTextContent("Your exact position is used for the check and then discarded");
    expect(notice).toHaveTextContent("has not been decided yet");
    expect(notice).toHaveTextContent("not proof of where anyone was");
    expect(await axe(notice)).toHaveNoViolations();
    expect(asked).not.toHaveBeenCalled();
  });
});

const EVIDENCE: components["schemas"]["LocationEvidenceView"] = {
  id: "evi-1",
  employee: {
    id: "e-2",
    name: "Test Worker",
    employee_code: "TEST-2",
    department: null,
    designation: null,
    location: null,
  },
  event_id: "ev-9",
  event_type: "CLOCK_IN",
  event_occurred_at: "2026-10-05T03:30:00Z",
  event_voided: false,
  work_date: "2026-10-05",
  location_id: "l-1",
  location_name: "Test office",
  outcome: "outside_radius",
  reason_codes: [],
  distance_m: 2000,
  distance_over_cap: false,
  accuracy_m: 15,
  position_age_seconds: 3,
  radius_m: 150,
  max_accuracy_m: 100,
  offices_considered: 1,
  received_at: "2026-10-05T03:30:01Z",
  current_review: null,
};

const HR_REVIEWER = {
  ...HR_ADMIN_ME,
  permissions: [
    ...HR_ADMIN_ME.permissions,
    "attendance.location.review.all",
    "attendance.location.assign.all",
    "attendance.policy.manage",
    "attendance.correction.create.all",
  ],
};

describe("location reviews", () => {
  function reviewData() {
    api
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/attendance/location-reviews", json({ items: [EVIDENCE], next_cursor: null }))
      .on(
        "GET /api/v1/attendance/location-reviews/evi-1",
        json({
          evidence: EVIDENCE,
          day_events: [
            {
              id: "ev-9",
              event_type: "CLOCK_IN",
              occurred_at: "2026-10-05T03:30:00Z",
              source: "web",
              voided: false,
              correction_id: null,
            },
          ],
          reviews: [],
          can_review: true,
        }),
      );
  }

  it("lists flagged events and records a decision with the review it replaces", async () => {
    reviewData();
    api.on(
      "POST /api/v1/attendance/location-reviews/evi-1/decisions",
      json(
        {
          id: "r-1",
          decision: "accepted",
          reason_code: "on_site_confirmed",
          note: null,
          reviewed_by: null,
          reviewed_at: "2026-10-06T05:00:00Z",
          supersedes: null,
        },
        201,
      ),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_REVIEWER, "/attendance/location-reviews");
    const table = await screen.findByRole("table", { name: "Location reviews" });
    expect(within(table).getByText("Reported location outside the office")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: /Review/ }));
    const dialog = await screen.findByRole("dialog", { name: /Location review: Test Worker/ });
    expect(dialog).toHaveTextContent("about 2000 m from the office (radius 150 m), reported accuracy 15 m");
    expect(dialog).not.toHaveTextContent(/latitude|longitude/i);
    expect(await axe(dialog)).toHaveNoViolations();
    await user.click(within(dialog).getByRole("button", { name: /Decision/ }));
    await user.click(await screen.findByRole("option", { name: "Accept the clock event as it is" }));
    await user.click(within(dialog).getByRole("button", { name: /Reason/ }));
    expect(screen.queryByRole("option", { name: /Not on site/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "On site, confirmed another way" }));
    await user.click(within(dialog).getByRole("button", { name: "Record review" }));
    expect(await screen.findByText("Review recorded")).toBeInTheDocument();
    expect(api.called("POST /api/v1/attendance/location-reviews/evi-1/decisions")[0]?.body).toEqual({
      decision: "accepted",
      reason_code: "on_site_confirmed",
      supersedes: null,
    });
  });

  it("shows a stale decision conflict and offers the correction workflow", async () => {
    reviewData();
    api
      .on(
        "POST /api/v1/attendance/location-reviews/evi-1/decisions",
        problem("conflict", 409, {
          code: "location.review_stale",
          detail: "Someone else reviewed this in the meantime. Reload to see their decision.",
        }),
      )
      .on(
        "GET /api/v1/employees/e-2/attendance/days",
        json({ time_zone: "Asia/Kolkata", items: [attendanceDay({ work_date: "2026-10-05" })] }),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_REVIEWER, "/attendance/location-reviews");
    await user.click(await screen.findByRole("button", { name: /Review/ }));
    const dialog = await screen.findByRole("dialog", { name: /Location review/ });
    await user.click(within(dialog).getByRole("button", { name: /Decision/ }));
    await user.click(await screen.findByRole("option", { name: "Do not accept it" }));
    await user.click(within(dialog).getByRole("button", { name: /Reason/ }));
    await user.click(
      await screen.findByRole("option", { name: "Not on site: attendance needs a correction" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Record review" }));
    expect(await within(dialog).findByText(/Someone else reviewed this/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Record a correction" }));
    expect(await screen.findByRole("dialog", { name: "Record a correction" })).toBeInTheDocument();
  });

  it("is not reachable for managers", async () => {
    renderSignedIn(api, MANAGER_ME, "/attendance/location-reviews");
    expect(
      await screen.findByRole("heading", { name: "You don't have access to this page" }),
    ).toBeInTheDocument();
  });
});

describe("office locations set-up", () => {
  function setupData() {
    api
      .on(
        "GET /api/v1/attendance/setup",
        json({
          without_shift: [],
          without_policy: [],
          shifts_without_policy: [],
          locations_without_policy: [],
        }),
      )
      .on(
        "GET /api/v1/locations",
        json({ items: [{ id: "l-1", code: "TEST-L", name: "Test office", status: "active" }] }),
      )
      .on("GET /api/v1/attendance/location-checks", json({ items: [], checks_enabled: true }));
  }

  it("adds an office check from this browser's position, with no suggested values filled in", async () => {
    setupData();
    browser("granted", 8);
    api.on(
      "POST /api/v1/attendance/location-checks",
      json(
        {
          id: "c-1",
          location_id: "l-1",
          location_code: "TEST-L",
          location_name: "Test office",
          effective_from: "2026-10-05",
          effective_to: null,
          mode: "verify",
          latitude: 12.0001,
          longitude: 77.0002,
          radius_m: 150,
          max_accuracy_m: 100,
          max_position_age_seconds: 120,
          review_unverified: true,
          created_at: "2026-10-05T00:00:00Z",
        },
        201,
      ),
    );
    const user = userEvent.setup();
    renderSignedIn(api, HR_REVIEWER, "/setup/attendance");
    await user.click(await screen.findByRole("tab", { name: "Office locations" }));
    expect(await screen.findByText("No office has a location check")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add an office check" }));
    const dialog = await screen.findByRole("dialog", { name: "Add an office check" });
    expect(within(dialog).getByLabelText("Radius (m)")).toHaveValue("");
    expect(dialog).toHaveTextContent(
      "Proposed starting values for the pilot: a 150 m radius and 100 m accepted accuracy",
    );
    await user.click(within(dialog).getByRole("button", { name: "Use my current position" }));
    expect(await within(dialog).findByText(/accurate to about 8 m/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Latitude")).toHaveValue("12.000100");
    await user.click(within(dialog).getByRole("button", { name: /Office/ }));
    await user.click(await screen.findByRole("option", { name: "Test office" }));
    await user.type(within(dialog).getByLabelText("Radius (m)"), "150");
    await user.type(within(dialog).getByLabelText("Accepted accuracy (m)"), "100");
    await user.type(within(dialog).getByLabelText("Maximum position age (s)"), "120");
    await user.click(within(dialog).getByRole("button", { name: "Save version" }));
    expect(await screen.findByText("Office check saved")).toBeInTheDocument();
    expect(api.called("POST /api/v1/attendance/location-checks")[0]?.body).toMatchObject({
      location_id: "l-1",
      mode: "verify",
      latitude: 12.0001,
      radius_m: 150,
      max_accuracy_m: 100,
      max_position_age_seconds: 120,
      review_unverified: true,
    });
  });

  it("approves several people for an office and ends an approval, without deleting it", async () => {
    const check = {
      id: "c-1",
      location_id: "l-1",
      location_code: "TEST-L",
      location_name: "Test office",
      effective_from: "2026-01-01",
      effective_to: null,
      mode: "verify" as const,
      latitude: 12,
      longitude: 77,
      radius_m: 150,
      max_accuracy_m: 100,
      max_position_age_seconds: 120,
      review_unverified: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    const approval = {
      id: "a-1",
      employee_id: "e-2",
      employee_name: "Test Worker",
      employee_code: "TEST-2",
      location_id: "l-1",
      location_name: "Test office",
      effective_from: "2026-01-01",
      effective_to: null,
      created_at: "2026-01-01T00:00:00Z",
    };
    api
      .on(
        "GET /api/v1/attendance/setup",
        json({
          without_shift: [],
          without_policy: [],
          shifts_without_policy: [],
          locations_without_policy: [],
        }),
      )
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/attendance/location-checks", json({ items: [check], checks_enabled: true }))
      .on("GET /api/v1/attendance/office-approvals", json({ items: [approval] }))
      .on(
        "POST /api/v1/attendance/office-approvals/a-1/end",
        json({ ...approval, effective_to: "2099-01-01" }),
      );
    const user = userEvent.setup();
    renderSignedIn(api, HR_REVIEWER, "/setup/attendance");
    await user.click(await screen.findByRole("tab", { name: "Office locations" }));
    expect(await screen.findByText(/Checks are on: within 150 m/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approved people" }));
    const dialog = await screen.findByRole("dialog", { name: "People approved for Test office" });
    expect(dialog).toHaveTextContent("Everyone whose job is at Test office may clock there already");
    expect(within(dialog).getByText("Test Worker")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /End the approval of Test Worker/ }));
    expect(await screen.findByText("Approval ended")).toBeInTheDocument();
    expect(api.called("GET /api/v1/attendance/office-approvals")[0]?.query.get("location_id")).toBe("l-1");
  });

  it("says when the installation has checks turned off and offers no change", async () => {
    api
      .on(
        "GET /api/v1/attendance/setup",
        json({
          without_shift: [],
          without_policy: [],
          shifts_without_policy: [],
          locations_without_policy: [],
        }),
      )
      .on("GET /api/v1/locations", json({ items: [] }))
      .on("GET /api/v1/attendance/location-checks", json({ items: [], checks_enabled: false }));
    const user = userEvent.setup();
    renderSignedIn(api, HR_REVIEWER, "/setup/attendance");
    await user.click(await screen.findByRole("tab", { name: "Office locations" }));
    expect(await screen.findByText(/turned off for this installation/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add an office check" })).not.toBeInTheDocument();
  });
});
