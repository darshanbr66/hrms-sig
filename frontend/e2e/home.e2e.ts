/**
 * The public home page at /: it fits phones, tablets and desktops without sideways scrolling,
 * passes axe in both themes, and sends no API request at all, so a sleeping or failing API
 * cannot change it. The sign-in link leads to /sign-in, where the readiness check starts.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const VIEWPORTS = [
  { name: "small phone", width: 360, height: 740 },
  { name: "phone", width: 390, height: 844 },
  { name: "tablet", width: 820, height: 1180 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const viewport of VIEWPORTS) {
  test(`the home page fits a ${viewport.name} screen without calling the API`, async ({ page }) => {
    const requests: string[] = [];
    // The API is down: every request fails. The page must not notice.
    await page.route("**/api/**", async (route) => {
      requests.push(route.request().url());
      await route.abort();
    });
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/");
    await expect(
      page.getByRole("heading", { level: 1, name: "People first. Work, in order." }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Employee sign in" }).first()).toBeInViewport();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.waitForLoadState("networkidle");
    expect(requests).toEqual([]);
    await expect(page.getByText(/couldn't reach the HRMS/)).toHaveCount(0);
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`the home page has no accessibility violations in the ${theme} theme`, async ({ page }) => {
    await page.route("**/api/**", (route) => route.abort());
    await page.addInitScript((value) => {
      localStorage.setItem("sv-theme", value);
    }, theme);
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations).toEqual([]);
  });
}

test("keyboard users can skip to the content and reach sign-in", async ({ page }) => {
  await page.route("**/api/**", (route) => route.abort());
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Tab");
  const signIn = page.getByRole("link", { name: "Employee sign in" }).first();
  await expect(signIn).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/sign-in$/);
  await expect(page.getByRole("heading", { name: "Sign in", level: 1 })).toBeVisible();
});
