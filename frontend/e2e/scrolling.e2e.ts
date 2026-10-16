/**
 * Scrolling in a real browser: the page scrolls by wheel and keyboard; opening a dialog does
 * not shift the page sideways; a dialog's body scrolls without moving the page behind it; a
 * table header stays below the top bar; focus is never hidden under the top bar; no page
 * scrolls sideways on a phone; scrollbars are slim and themed, never hidden. All data is fake.
 */
import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./support";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", fakeApi);
});

async function openCalendar(page: Page, height = 420) {
  await page.setViewportSize({ width: 1280, height });
  await page.goto("/setup/holiday-calendars/c-1");
  await expect(page.getByRole("table", { name: "Holidays" })).toBeVisible();
}

const scrollY = (page: Page) => page.evaluate(() => window.scrollY);

test("the page scrolls by wheel and keyboard", async ({ page }) => {
  await openCalendar(page);
  await page.mouse.move(640, 300);
  await page.mouse.wheel(0, 300);
  await expect.poll(() => scrollY(page)).toBeGreaterThan(0);
  await page.evaluate(() => {
    window.scrollTo(0, 0);
  });
  await page.locator("main").click({ position: { x: 5, y: 5 } });
  await page.keyboard.press("End");
  await expect.poll(() => scrollY(page)).toBeGreaterThan(0);
});

test("opening a dialog neither shifts the page sideways nor lets the page scroll behind it", async ({
  page,
}) => {
  await openCalendar(page, 360);
  const heading = page.getByRole("heading", { level: 1 });
  const before = await heading.boundingBox();
  await page.getByRole("button", { name: "Add a date" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a date" });
  await expect(dialog).toBeVisible();
  const after = await heading.boundingBox();
  expect(after?.x).toBeCloseTo(before?.x ?? -1, 0);

  const pageBefore = await scrollY(page);
  const body = dialog.locator("[data-dialog-body]");
  const box = await body.boundingBox();
  if (box) await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 2000);
  await expect.poll(() => body.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  // Scrolling past the end of the dialog does not carry on into the page.
  await page.mouse.wheel(0, 2000);
  expect(await scrollY(page)).toBe(pageBefore);
});

test("a table header stays below the top bar while the page scrolls", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 300 });
  await page.goto("/setup/holiday-calendars/c-1");
  const table = page.getByRole("table", { name: "Holidays" });
  await expect(table).toBeVisible();
  const header = table.getByRole("columnheader", { name: "Date" });
  await table.evaluate((element) => {
    window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 20);
  });
  await page.mouse.wheel(0, 40);
  await expect.poll(async () => (await header.boundingBox())?.y).toBeCloseTo(56, 0);
  const topBar = await page.locator("header").first().boundingBox();
  expect(topBar?.height).toBe(56);
});

test("keyboard focus is never hidden under the top bar", async ({ page }) => {
  await openCalendar(page, 300);
  await page.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
  });
  const edit = page.getByRole("button", { name: "Edit Test holiday" });
  await edit.focus();
  const box = await edit.boundingBox();
  expect(box?.y ?? 0).toBeGreaterThanOrEqual(56);
});

for (const path of [
  "/",
  "/notifications",
  "/setup/holiday-calendars/c-1",
  "/setup/leave-types",
  "/organization",
]) {
  test(`${path} does not scroll sideways on a phone`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 664 });
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    ).toBeLessThanOrEqual(0);
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`scrollbars are slim and themed in the ${theme} theme`, async ({ page }) => {
    await page.addInitScript((value) => {
      window.localStorage.setItem("sv-theme", value);
    }, theme);
    await openCalendar(page);
    const styles = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      const nav = document.querySelector("nav[data-scroll-area]");
      return {
        width: root.scrollbarWidth,
        color: root.scrollbarColor,
        gutter: root.scrollbarGutter,
        nav: nav ? getComputedStyle(nav).overscrollBehaviorY : null,
      };
    });
    const thumb = theme === "light" ? "rgb(168, 162, 154)" : "rgb(98, 92, 86)";
    expect(styles).toEqual({
      width: "thin",
      color: `${thumb} rgba(0, 0, 0, 0)`,
      gutter: "stable",
      nav: "contain",
    });
  });
}
