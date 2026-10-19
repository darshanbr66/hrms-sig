/**
 * The production build runs under the production headers from vercel.json without a single
 * Content-Security-Policy violation: pages, a dialog and a theme change. The SPA's policy allows
 * no inline script or style; the only exceptions are the two fixed `<style>` elements React Aria
 * injects (usePress and, on iOS, usePreventScroll), allowed by hash. A React Aria upgrade that
 * changes them fails here instead of silently losing those styles in production.
 * Run with `pnpm test:csp` (it builds first). All data is fake.
 */
import { expect, test, type Page } from "@playwright/test";

import vercel from "../vercel.json" with { type: "json" };
import { fakeApi } from "./support";

// The rule that applies to every page of the SPA (everything outside /api/).
const pageRule = vercel.headers.find((rule) => rule.source === "/((?!api/).*)");
const pageHeaders = Object.fromEntries(
  (pageRule?.headers ?? []).map(({ key, value }) => [key.toLowerCase(), value]),
);

async function violations(page: Page) {
  return page.evaluate(() => (window as unknown as { __violations?: string[] }).__violations ?? []);
}

test.beforeEach(async ({ page }) => {
  expect(pageHeaders["content-security-policy"]).toContain("script-src 'self'");
  await page.route("**/api/**", fakeApi);
  // Documents get the vercel.json headers, as Vercel serves them.
  await page.route(/^http:\/\/localhost:\d+\/(?!api\/|assets\/|theme-init\.js).*$/, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), ...pageHeaders } });
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      const store = window as unknown as { __violations?: string[] };
      store.__violations = [
        ...(store.__violations ?? []),
        `${event.violatedDirective} from ${event.sourceFile}:${event.lineNumber}`,
      ];
    });
  });
});

test("pages, a dialog and a theme change cause no policy violation", async ({ page }) => {
  for (const path of [
    "/",
    "/setup/holiday-calendars/c-1",
    "/setup/leave-types",
    "/organization?tab=departments",
  ]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(await violations(page), path).toEqual([]);
  }
  await page.goto("/setup/holiday-calendars/c-1");
  await page.getByRole("button", { name: "Add a date" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");
  expect(await violations(page)).toEqual([]);
  // React Aria's touch-action rule was allowed, not blocked.
  expect(await page.evaluate(() => document.head.querySelector("style")?.textContent ?? "")).toContain(
    "touch-action",
  );
});

test("the iOS scroll-lock style is allowed by its hash", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  // The exact element usePreventScroll adds on iOS while a dialog is open; desktop Chromium
  // never takes that path, so the test adds it the same way.
  await page.evaluate(() => {
    const style = document.createElement("style");
    style.textContent = "@layer {\n  * {\n    overscroll-behavior: contain;\n  }\n}";
    document.head.prepend(style);
  });
  expect(await page.evaluate(() => getComputedStyle(document.body).overscrollBehavior)).toBe("contain");
  expect(await violations(page)).toEqual([]);
});
