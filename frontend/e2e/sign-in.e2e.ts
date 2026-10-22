/**
 * The sign-in screen on phones, tablets and desktops: the form fits without sideways scrolling,
 * the introduction sits to the left of the form only on wide screens (a short masthead above
 * it otherwise), the connection status follows the
 * readiness check, and axe finds nothing in either theme. All data is fake.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { fakeApi, signedOutApi } from "./support";

const VIEWPORTS = [
  { name: "phone", width: 390, height: 844, panel: false },
  { name: "tablet", width: 820, height: 1180, panel: false },
  { name: "laptop", width: 1366, height: 768, panel: true },
  { name: "desktop", width: 1920, height: 1080, panel: true },
];

for (const viewport of VIEWPORTS) {
  test(`sign-in fits a ${viewport.name} screen`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route("**/api/**", signedOutApi);
    await page.goto("/sign-in");
    await expect(page.getByRole("heading", { name: "Sign in", level: 1 })).toBeVisible();
    await expect(page.getByText("Connected to HRMS")).toBeVisible();

    for (const name of ["Work email", "Password"]) await expect(page.getByLabel(name)).toBeInViewport();
    await expect(page.getByRole("button", { name: "Continue" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Forgot your password?" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);

    const panel = page.getByRole("complementary", { name: "About Sigvitas HRMS" });
    if (viewport.panel) {
      await expect(panel).toBeVisible();
      const form = await page.getByLabel("Work email").boundingBox();
      const aside = await panel.boundingBox();
      expect(form && aside && aside.x + aside.width <= form.x).toBe(true);
    } else {
      await expect(panel).toBeHidden();
      // The masthead stays short, so the form starts within the first screen.
      const email = await page.getByLabel("Work email").boundingBox();
      expect(email && email.y + email.height <= viewport.height).toBe(true);
    }
  });
}

test("the sign-in screen has no accessibility violations in either theme", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.route("**/api/**", signedOutApi);
  for (const theme of ["light", "dark"]) {
    await page.addInitScript((value) => {
      localStorage.setItem("sv-theme", value);
    }, theme);
    await page.goto("/sign-in");
    await expect(page.getByText("Connected to HRMS")).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, theme).toEqual([]);
  }
});

test("shows that the HRMS is starting, then offers Retry when it does not answer", async ({ page }) => {
  let awake = false;
  await page.route("**/api/**", async (route) => {
    if (new URL(route.request().url()).pathname !== "/api/health/ready") return signedOutApi(route);
    if (awake)
      return route.fulfill({ status: 200, contentType: "application/json", body: '{"status":"ok"}' });
    // A gateway that cannot reach a sleeping API.
    return route.fulfill({ status: 504, contentType: "text/plain", body: "Gateway timeout" });
  });
  await page.clock.install();
  await page.goto("/sign-in");
  await expect(page.getByText(/starting up after a quiet period/)).toBeVisible();
  // Each answer arrives in real time and schedules the next pause, so time moves in steps.
  const retry = page.getByRole("button", { name: "Retry" });
  await expect(async () => {
    await page.clock.runFor(16_000);
    await expect(retry).toBeVisible({ timeout: 250 });
  }).toPass({ timeout: 30_000 });
  const checks = await page.evaluate(
    () => performance.getEntriesByName(`${location.origin}/api/health/ready`).length,
  );
  expect(checks).toBeLessThanOrEqual(8);
  awake = true;
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Connected to HRMS")).toBeVisible();
});

test("a browser that still holds a valid session goes from the home page to the dashboard", async ({
  page,
}) => {
  // The fake API answers /api/v1/me as a signed-in HR admin: the session the browser kept.
  await page.route("**/api/**", fakeApi);
  await page.goto("/");
  await page.getByRole("link", { name: "Employee sign in" }).first().click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
});

test("without a session, sign-in asks for the password once and does not keep refreshing", async ({
  page,
}) => {
  const refreshes: string[] = [];
  await page.route("**/api/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/refresh") refreshes.push("refresh");
    return signedOutApi(route);
  });
  await page.goto("/sign-in");
  await expect(page.getByLabel("Work email")).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(page).toHaveURL(/\/sign-in$/);
  expect(refreshes).toHaveLength(1);
});
