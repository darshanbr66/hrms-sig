/**
 * Every add dialog fits the visible viewport: its title and action buttons are on screen and
 * its body scrolls, on small laptops, desktops, zoomed pages and phones. All data is fake.
 */
import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./support";

const VIEWPORTS = [
  { name: "small laptop", width: 1280, height: 600 },
  { name: "laptop", width: 1366, height: 768 },
  { name: "desktop", width: 1920, height: 1080 },
  // A 1280 x 720 window at 200 % zoom lays out like 640 x 360 CSS pixels.
  { name: "zoomed 200 %", width: 640, height: 360 },
  { name: "phone", width: 390, height: 664 },
];

const DIALOGS: { path: string; tab?: string; open: string | RegExp; title: string | RegExp }[] = [
  { path: "/setup/attendance", tab: "Shifts", open: "Add shift", title: "Add shift" },
  { path: "/setup/attendance", tab: "Policies", open: "Add policy version", title: /policy/i },
  { path: "/setup/leave-types", open: "Add leave type", title: "Add leave type" },
  { path: "/organization?tab=departments", open: "Add department", title: "Add department" },
  { path: "/organization?tab=locations", open: "Add location", title: "Add location" },
  { path: "/setup/holiday-calendars", open: /Add (holiday )?calendar/, title: /calendar/i },
  { path: "/setup/holiday-calendars/c-1", open: "Add a date", title: "Add a date" },
];

async function inViewport(page: Page, selector: string, dialog: ReturnType<Page["getByRole"]>) {
  const box = await dialog.locator(selector).first().boundingBox();
  const size = page.viewportSize();
  expect(box, `${selector} has a box`).not.toBeNull();
  if (!box || !size) return;
  expect(box.y, `${selector} starts inside the viewport`).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height, `${selector} ends inside the viewport`).toBeLessThanOrEqual(size.height + 0.5);
}

for (const viewport of VIEWPORTS) {
  for (const item of DIALOGS) {
    test(`${String(item.open)} fits a ${viewport.name} screen`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.route("**/api/**", fakeApi);
      await page.goto(item.path);
      if (item.tab) await page.getByRole("tab", { name: item.tab, exact: true }).click();
      await page.getByRole("button", { name: item.open }).first().click();
      const dialog = page.getByRole("dialog", { name: item.title });
      await expect(dialog).toBeVisible();

      await inViewport(page, "h2, [slot=title]", dialog);
      await inViewport(page, "[data-dialog-actions]", dialog);
      const actionButtons = dialog.locator("[data-dialog-actions] button");
      expect(await actionButtons.count()).toBeGreaterThanOrEqual(2);
      for (const button of await actionButtons.all()) await expect(button).toBeInViewport();

      // The body scrolls, and at either end of it the first and last real fields sit fully in
      // view, clear of the pinned action row. React Aria keeps native inputs and selects
      // visually hidden (1 px), so those are skipped.
      const body = dialog.locator("[data-dialog-body]");
      for (const end of ["bottom", "top"] as const) {
        const placement = await body.evaluate((element, which) => {
          element.scrollTop = which === "bottom" ? element.scrollHeight : 0;
          const fields = [...element.querySelectorAll("label, legend, textarea, button")]
            .filter((node) => !node.closest('[data-dialog-actions], [aria-hidden="true"]'))
            .map((node) => node.getBoundingClientRect())
            .filter((rect) => rect.width > 2 && rect.height > 2);
          const field = which === "bottom" ? fields.at(-1) : fields[0];
          const actions = element.querySelector("[data-dialog-actions]")?.getBoundingClientRect();
          const area = element.getBoundingClientRect();
          return field && actions
            ? {
                top: field.top,
                bottom: field.bottom,
                areaTop: area.top,
                actionsTop: actions.top,
                actionsBottom: actions.bottom,
                areaBottom: area.bottom,
              }
            : null;
        }, end);
        expect(placement, `a field at the ${end}`).not.toBeNull();
        if (!placement) continue;
        expect(placement.top).toBeGreaterThanOrEqual(placement.areaTop - 0.5);
        expect(placement.bottom).toBeLessThanOrEqual(placement.actionsTop + 0.5);
        // The action row closes the body: nothing can scroll into view beneath it.
        expect(Math.abs(placement.areaBottom - placement.actionsBottom)).toBeLessThanOrEqual(1);
      }
      await inViewport(page, "[data-dialog-actions]", dialog);

      // Keyboard: focus starts inside, Tab stays inside, Escape closes.
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
    });
  }
}
