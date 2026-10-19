/**
 * Non-text contrast of form fields (WCAG 2.2 SC 1.4.11), measured in a real browser. axe checks
 * text contrast only, so this test reads each visible field's rendered border colour and
 * compares it with the field's own fill and with the surface behind the field: both must reach
 * 3:1, in the light and the dark theme. It covers the shared text field, text area, select,
 * combo box and search field on working screens and dialogs, and the view switch, whose chosen
 * button must be marked by more than a colour change (SC 1.4.11 for states, SC 1.4.1). All data
 * is fake.
 */
import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./support";

interface Measured {
  kind: string;
  name: string;
  againstFill: number;
  againstSurface: number;
}

/** Runs in the page: every visible field under `root`, its kind, and its two border ratios. */
function measureFields(root: Element): Measured[] {
  const parse = (value: string): [number, number, number, number] => {
    const parts = value.match(/[\d.]+/g)?.map(Number) ?? [];
    return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0, parts[3] ?? 1];
  };
  const luminance = ([r, g, b]: number[]) => {
    const channel = (c: number) => {
      const v = c / 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(r ?? 0) + 0.7152 * channel(g ?? 0) + 0.0722 * channel(b ?? 0);
  };
  const ratio = (a: number[], b: number[]) => {
    const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
    return Math.round(((high + 0.05) / (low + 0.05)) * 100) / 100;
  };
  // The first opaque background at or above the element.
  const surface = (start: Element | null): number[] => {
    for (let node = start; node; node = node.parentElement) {
      const colour = parse(getComputedStyle(node).backgroundColor);
      if (colour[3] >= 1) return colour;
    }
    return parse(getComputedStyle(document.body).backgroundColor);
  };
  const kindOf = (el: Element) => {
    if (el.tagName === "TEXTAREA") return "text area";
    if (el.tagName === "BUTTON") return "select";
    if (el.getAttribute("role") === "combobox") return "combo box";
    if (el.getAttribute("type") === "search") return "search";
    return "text";
  };
  const selector =
    'input:not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="hidden"]), textarea, button[aria-haspopup="listbox"]';
  return [...root.querySelectorAll(selector)]
    .filter((el) => {
      const box = el.getBoundingClientRect();
      return el.checkVisibility() && box.width > 4 && box.height > 4;
    })
    .map((el) => {
      const border = parse(getComputedStyle(el).borderTopColor);
      return {
        kind: kindOf(el),
        name: el.getAttribute("aria-label") ?? el.id,
        againstFill: ratio(border, surface(el)),
        againstSurface: ratio(border, surface(el.parentElement)),
      };
    });
}

const DEPARTMENT = {
  id: "d-1",
  code: "TST",
  name: "Test department",
  description: null,
  parent_id: null,
  head_name: null,
  head_employee_id: null,
  status: "active",
  version: 1,
  created_at: "2031-01-01T00:00:00Z",
  updated_at: "2031-01-01T00:00:00Z",
};

/** Contrast ratio of two computed `rgb(…)` colours. */
function contrast(a: string, b: string): number {
  const luminance = (value: string) =>
    (value.match(/\d+/g) ?? [])
      .slice(0, 3)
      .map((c) => Number(c) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
      .reduce((sum, v, i) => sum + v * ([0.2126, 0.7152, 0.0722][i] ?? 0), 0);
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (high + 0.05) / (low + 0.05);
}

async function setTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("sv-theme", value);
  }, theme);
}

const SCREENS: { name: string; path: string; open?: string; kinds: string[] }[] = [
  {
    name: "the add a date dialog",
    path: "/setup/holiday-calendars/c-1",
    open: "Add a date",
    kinds: ["select", "text"],
  },
  {
    name: "the add leave type dialog",
    path: "/setup/leave-types",
    open: "Add leave type",
    kinds: ["select", "text", "text area"],
  },
  { name: "the departments list", path: "/organization?tab=departments", kinds: ["search"] },
  {
    name: "the add department dialog",
    path: "/organization?tab=departments",
    open: "Add department",
    kinds: ["select", "text", "text area", "combo box"],
  },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", fakeApi);
});

for (const theme of ["light", "dark"] as const) {
  test(`the field border token reaches 3:1 against every surface in the ${theme} theme`, async ({ page }) => {
    await setTheme(page, theme);
    await page.goto("/");
    await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
    const ratios = await page.evaluate(() => {
      const probe = document.createElement("div");
      document.body.append(probe);
      const colour = (token: string) => {
        probe.style.color = `var(${token})`;
        return getComputedStyle(probe).color;
      };
      const border = colour("--color-control-border");
      const surfaces = ["--color-neutral-0", "--color-neutral-50", "--color-neutral-100"].map(colour);
      probe.remove();
      return { border, surfaces };
    });
    const parse = (value: string) => (value.match(/\d+/g) ?? []).slice(0, 3).map(Number);
    const luminance = (rgb: number[]) =>
      rgb
        .map((c) => c / 255)
        .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
        .reduce((sum, v, i) => sum + v * ([0.2126, 0.7152, 0.0722][i] ?? 0), 0);
    const border = luminance(parse(ratios.border));
    for (const surface of ratios.surfaces) {
      const other = luminance(parse(surface));
      const contrast = (Math.max(border, other) + 0.05) / (Math.min(border, other) + 0.05);
      expect(contrast, `${ratios.border} on ${surface}`).toBeGreaterThanOrEqual(3);
    }
  });

  for (const screen of SCREENS) {
    test(`fields on ${screen.name} have 3:1 borders in the ${theme} theme`, async ({ page }) => {
      await setTheme(page, theme);
      await page.goto(screen.path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      if (screen.open) {
        await page.getByRole("button", { name: screen.open }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
      }
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
      const scope = screen.open ? page.getByRole("dialog") : page.locator("main");
      const fields = await scope.evaluate(measureFields);
      expect(new Set(fields.map((field) => field.kind))).toEqual(new Set(screen.kinds));
      const weak = fields.filter((field) => field.againstFill < 3 || field.againstSurface < 3);
      expect(weak).toEqual([]);
    });
  }

  test(`the chosen view is marked by a 3:1 bar, not colour alone, in the ${theme} theme`, async ({
    page,
  }) => {
    await setTheme(page, theme);
    // Registered after the shared fake API, so it answers the departments request first.
    await page.route("**/api/v1/departments*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [DEPARTMENT], next_cursor: null }),
      }),
    );
    await page.goto("/organization?tab=departments");
    const chosen = page.getByRole("radio", { name: "List" });
    const other = page.getByRole("radio", { name: "Hierarchy" });
    await expect(chosen).toBeChecked();
    const marks = await page.evaluate(() => {
      const radios = [...document.querySelectorAll('[role="radio"]')];
      const style = (checked: boolean) => {
        const el = radios.find((r) => (r.getAttribute("aria-checked") === "true") === checked);
        return el ? getComputedStyle(el) : null;
      };
      const on = style(true);
      const off = style(false);
      return {
        onShadow: on?.boxShadow ?? "",
        offShadow: off?.boxShadow ?? "",
        onFill: on?.backgroundColor ?? "",
      };
    });
    expect(marks.offShadow).toBe("none");
    // A 2 px bar along the bottom edge (Tailwind adds transparent layers before it).
    const bar = /rgb\(\d+, \d+, \d+\)(?= 0px -2px 0px 0px inset)/.exec(marks.onShadow)?.[0];
    expect(bar).toBeDefined();
    if (!bar) return;
    expect(contrast(bar, marks.onFill)).toBeGreaterThanOrEqual(3);
    // The bar moves with the choice.
    await other.click();
    await expect(other).toBeChecked();
    expect(await other.evaluate((el) => getComputedStyle(el).boxShadow)).toMatch(/-2px 0px 0px inset$/);
    expect(await chosen.evaluate((el) => getComputedStyle(el).boxShadow)).toBe("none");
  });
}
