/**
 * The colour theme and the fonts, in a real browser: the stored or system theme is applied
 * before the app's code runs (no flash of the wrong theme), the account menu changes it and
 * the choice survives a reload, both themes pass axe's WCAG 2.2 AA checks (colour contrast
 * included) on working screens, and IBM Plex Sans and IBM Plex Mono are the fonts actually
 * loaded. All data is fake.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./support";

const THEME_KEY = "sv-theme";

function storeTheme(page: Page, theme: string | null) {
  return page.addInitScript(
    ([key, value]) => {
      // Only on the first load of this test, so a reload sees what the app stored.
      if (window.sessionStorage.getItem("seeded")) return;
      window.sessionStorage.setItem("seeded", "1");
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    },
    [THEME_KEY, theme] as const,
  );
}

const theme = (page: Page) => page.evaluate(() => document.documentElement.dataset.theme);

async function bodyBackground(page: Page) {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", fakeApi);
});

test("the stored theme is applied before the app starts", async ({ page }) => {
  await storeTheme(page, "dark");
  // Hold the app's code back: whatever the page shows meanwhile must already be dark.
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/src/main.tsx*", async (route) => {
    await held;
    await route.continue();
  });
  await page.goto("/", { waitUntil: "commit" });
  await page.waitForFunction(() => document.documentElement.dataset.theme !== undefined);
  expect(await theme(page)).toBe("dark");
  expect(await page.locator("#root").innerHTML()).toBe("");
  release();
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
  expect(await theme(page)).toBe("dark");
});

test("the system setting applies until a theme is chosen, and the choice is remembered", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await storeTheme(page, null);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
  expect(await theme(page)).toBe("dark");
  expect(await bodyBackground(page)).toBe("rgb(22, 21, 20)");

  // Keyboard only: open the account menu, choose Light.
  await page.getByRole("button", { name: "Account menu" }).focus();
  await page.keyboard.press("Enter");
  const themes = page.getByRole("group", { name: "Theme" });
  await expect(themes.getByRole("menuitemradio", { name: "Same as system" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await themes.getByRole("menuitemradio", { name: "Light" }).focus();
  await page.keyboard.press("Enter");
  expect(await theme(page)).toBe("light");
  await expect(themes.getByRole("menuitemradio", { name: "Light" })).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toBeHidden();
  await expect.poll(() => bodyBackground(page)).toBe("rgb(250, 249, 247)");

  await page.reload();
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
  expect(await theme(page)).toBe("light");
  expect(await page.evaluate((key) => window.localStorage.getItem(key), THEME_KEY)).toBe("light");

  // Following the system again: an operating-system change applies at once.
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitemradio", { name: "Same as system" }).click();
  expect(await theme(page)).toBe("dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect.poll(() => theme(page)).toBe("light");
});

test("a theme change does not animate when reduced motion is asked for", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await storeTheme(page, "light");
  await page.goto("/");
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  expect(await page.evaluate(() => document.documentElement.classList.contains("theme-changing"))).toBe(
    false,
  );
  // The colours are final straight away.
  expect(await bodyBackground(page)).toBe("rgb(22, 21, 20)");
});

const SCREENS: { name: string; path: string; open?: string }[] = [
  { name: "dashboard", path: "/" },
  { name: "holiday calendars", path: "/setup/holiday-calendars" },
  { name: "a holiday calendar", path: "/setup/holiday-calendars/c-1" },
  { name: "leave types", path: "/setup/leave-types" },
  { name: "organization", path: "/organization?tab=locations" },
  { name: "add a date dialog", path: "/setup/holiday-calendars/c-1", open: "Add a date" },
];

for (const scheme of ["light", "dark"] as const) {
  for (const screen of SCREENS) {
    test(`${screen.name} passes axe in the ${scheme} theme`, async ({ page }) => {
      await storeTheme(page, scheme);
      await page.goto(screen.path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      if (screen.open) {
        await page.getByRole("button", { name: screen.open }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
      }
      expect(await theme(page)).toBe(scheme);
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();
      expect(
        results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`),
      ).toEqual([]);
    });
  }
}

test("the account menu passes axe with the theme choices open, in the dark theme", async ({ page }) => {
  await storeTheme(page, "dark");
  await page.goto("/");
  await page.getByRole("button", { name: "Account menu" }).click();
  await expect(page.getByRole("group", { name: "Theme" })).toBeVisible();
  const results = await new AxeBuilder({ page }).include('[role="menu"]').analyze();
  expect(results.violations.map((v) => v.id)).toEqual([]);
});

test("IBM Plex Sans is the UI font and IBM Plex Mono the font for codes, both loaded", async ({ page }) => {
  await page.goto("/setup/leave-types");
  const code = page.locator("code", { hasText: "TL" });
  await expect(code).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() => ({
    body: getComputedStyle(document.body).fontFamily,
    code: getComputedStyle(document.querySelector("code") ?? document.body).fontFamily,
    loaded: [...document.fonts].filter((face) => face.status === "loaded").map((face) => face.family),
    sans: document.fonts.check('16px "IBM Plex Sans"'),
    mono: document.fonts.check('12px "IBM Plex Mono"'),
  }));
  expect(fonts.body.startsWith('"IBM Plex Sans"')).toBe(true);
  expect(fonts.code.startsWith('"IBM Plex Mono"')).toBe(true);
  expect(fonts.loaded).toEqual(expect.arrayContaining(["IBM Plex Sans", "IBM Plex Mono"]));
  expect(fonts.sans && fonts.mono).toBe(true);
});
