/**
 * Checkboxes in a real browser, on the notification preferences (the screen where the defect
 * was reported, 2026-10-16): a click shows the new state at once, the right value is sent,
 * the saved state survives a reload, a refused save puts the box back without claiming
 * success, repeated saves show one confirmation, and the box works by keyboard, in both
 * themes and at 200 % zoom. The API is a fake that keeps the saved preferences in memory.
 */
import { expect, test, type Page, type Route } from "@playwright/test";

import { fakeApi } from "./support";

const ACCENT_LIGHT = "rgb(38, 79, 143)";
const ACCENT_DARK = "rgb(142, 177, 234)";

interface Saved {
  channels: Record<string, Record<string, boolean>>;
  puts: unknown[];
  failNext: boolean;
}

function preferences(saved: Saved) {
  return {
    items: [
      {
        type: "leave.approval_requested",
        label: "Leave waiting for my decision",
        description: "When someone asks for leave you decide on.",
        channels: { ...saved.channels["leave.approval_requested"] },
      },
      {
        type: "export.ready",
        label: "Export ready",
        description: "When a file you asked for can be downloaded.",
        channels: { ...saved.channels["export.ready"] },
      },
    ],
  };
}

async function openPreferences(page: Page, theme: "light" | "dark" = "light") {
  const saved: Saved = {
    channels: {
      "leave.approval_requested": { in_app: true, email: false },
      "export.ready": { in_app: true },
    },
    puts: [],
    failNext: false,
  };
  await page.addInitScript((value) => {
    window.localStorage.setItem("sv-theme", value);
  }, theme);
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/me/notification-preferences") {
      if (request.method() === "PUT") {
        const body = request.postDataJSON() as {
          changes: { type: string; channel: string; enabled: boolean }[];
        };
        saved.puts.push(body);
        // A little latency, so the immediate (optimistic) state is what the test sees first.
        await new Promise((resolve) => setTimeout(resolve, 300));
        if (saved.failNext) {
          saved.failNext = false;
          return route.fulfill({
            status: 503,
            contentType: "application/problem+json",
            body: JSON.stringify({
              type: "/problems/service-unavailable",
              title: "The service is busy. Try again in a moment.",
              status: 503,
              request_id: "req-e2e",
            }),
          });
        }
        for (const change of body.changes) {
          const channels = saved.channels[change.type];
          if (channels) channels[change.channel] = change.enabled;
        }
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(preferences(saved)),
      });
    }
    return fakeApi(route);
  });
  await page.goto("/notifications");
  await page.getByRole("tab", { name: "What I receive" }).click();
  await expect(page.getByRole("list", { name: "Notification preferences" })).toBeVisible();
  return saved;
}

function emailBox(page: Page) {
  return page
    .getByRole("listitem")
    .filter({ hasText: "Leave waiting for my decision" })
    .getByRole("checkbox", { name: "By email" });
}

/** The drawn box beside the hidden input: its fill shows the state. */
async function boxFill(page: Page) {
  return emailBox(page).evaluate((input) => {
    const box = input.closest("label")?.querySelector("span[aria-hidden]");
    return box ? getComputedStyle(box).backgroundColor : null;
  });
}

test("a click shows the new state at once, sends it, and survives a reload", async ({ page }) => {
  const saved = await openPreferences(page);
  await expect(emailBox(page)).not.toBeChecked();
  const unchecked = await boxFill(page);
  expect(unchecked).not.toBe(ACCENT_LIGHT);

  await emailBox(page).click({ force: true });
  // Before the API answers (it waits 300 ms), the box is already checked and filled.
  await expect(emailBox(page)).toBeChecked({ timeout: 250 });
  await expect.poll(() => boxFill(page), { timeout: 250 }).toBe(ACCENT_LIGHT);
  expect(saved.puts).toHaveLength(1);
  await expect(page.getByText("Preference saved")).toBeVisible();
  expect(saved.puts).toEqual([
    { changes: [{ type: "leave.approval_requested", channel: "email", enabled: true }] },
  ]);

  await page.reload();
  await page.getByRole("tab", { name: "What I receive" }).click();
  await expect(emailBox(page)).toBeChecked();
  await expect.poll(() => boxFill(page)).toBe(ACCENT_LIGHT);
});

test("a refused save puts the box back and says it failed, without a success message", async ({ page }) => {
  const saved = await openPreferences(page);
  saved.failNext = true;
  await emailBox(page).click({ force: true });
  await expect(emailBox(page)).toBeChecked({ timeout: 250 });
  await expect(page.getByText("The service is busy. Try again in a moment.")).toBeVisible();
  await expect(emailBox(page)).not.toBeChecked();
  await expect(page.getByText("Preference saved")).toHaveCount(0);
  expect(saved.channels["leave.approval_requested"]?.email).toBe(false);
});

test("quick changes show one confirmation and end in the last state chosen", async ({ page }) => {
  const saved = await openPreferences(page);
  const inApp = page
    .getByRole("listitem")
    .filter({ hasText: "Export ready" })
    .getByRole("checkbox", { name: "In the app" });
  await emailBox(page).click({ force: true });
  await inApp.click({ force: true });
  await emailBox(page).click({ force: true });
  await expect(emailBox(page)).not.toBeChecked();
  await expect(inApp).not.toBeChecked();
  await expect.poll(() => saved.puts.length).toBe(3);
  await expect(page.getByText("Preference saved")).toHaveCount(1);
  // After every answer is in, the boxes still show the last choices, which are what was saved.
  await page.waitForTimeout(500);
  await expect(emailBox(page)).not.toBeChecked();
  await expect(inApp).not.toBeChecked();
  expect(saved.channels).toEqual({
    "leave.approval_requested": { in_app: true, email: false },
    "export.ready": { in_app: false },
  });
});

test("the checkbox works from the keyboard with a visible focus ring", async ({ page }) => {
  const saved = await openPreferences(page);
  await page.getByRole("tab", { name: "What I receive" }).focus();
  // Tab from the tab strip to the first box, then on to "By email".
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(emailBox(page)).toBeFocused();
  const ring = await emailBox(page).evaluate((input) => {
    const box = input.closest("label")?.querySelector("span[aria-hidden]");
    const style = box ? getComputedStyle(box) : null;
    return style ? { width: style.outlineWidth, style: style.outlineStyle, color: style.outlineColor } : null;
  });
  expect(ring).toEqual({ width: "2px", style: "solid", color: ACCENT_LIGHT });
  await page.keyboard.press("Space");
  await expect(emailBox(page)).toBeChecked();
  await expect.poll(() => saved.puts.length).toBe(1);
});

test("the checked state is clear in the dark theme", async ({ page }) => {
  await openPreferences(page, "dark");
  await emailBox(page).click({ force: true });
  await expect(emailBox(page)).toBeChecked();
  await expect.poll(() => boxFill(page)).toBe(ACCENT_DARK);
});

test("checkboxes work at 200 % zoom", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 360 });
  await openPreferences(page);
  await emailBox(page).scrollIntoViewIfNeeded();
  // A real click on the label, not a forced one: nothing covers it at this size.
  await page
    .getByRole("listitem")
    .filter({ hasText: "Leave waiting for my decision" })
    .getByText("By email")
    .click();
  await expect(emailBox(page)).toBeChecked();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("selected tabs are marked and the tab strip has no scrollbar of its own", async ({ page }) => {
  await openPreferences(page);
  const strip = page.getByRole("tablist", { name: "Notifications" });
  const overflow = await strip.evaluate((element) => ({
    vertical: element.scrollHeight - element.clientHeight,
    horizontal: element.scrollWidth - element.clientWidth,
  }));
  expect(overflow).toEqual({ vertical: 0, horizontal: 0 });
  const selected = page.getByRole("tab", { name: "What I receive" });
  await expect(selected).toHaveAttribute("aria-selected", "true");
  expect(await selected.evaluate((tab) => getComputedStyle(tab).borderBottomColor)).toBe(ACCENT_LIGHT);
  const other = page.getByRole("tab", { name: "Inbox" });
  expect(await other.evaluate((tab) => getComputedStyle(tab).borderBottomColor)).not.toBe(ACCENT_LIGHT);
});
