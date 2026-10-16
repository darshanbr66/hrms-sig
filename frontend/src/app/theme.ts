/**
 * The colour theme (docs/ui-ux-guidelines.md §4): light, dark, or the system's setting, chosen
 * in the account menu and remembered in this browser only. It is a display preference, not
 * account data, so it never reaches the API. `public/theme-init.js` applies the stored choice
 * before the first paint; this module keeps it applied and follows system changes.
 */
import { useCallback, useEffect, useSyncExternalStore } from "react";

export type ThemePreference = "light" | "dark" | "system";

export const THEME_KEY = "sv-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";
const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
const CHANGE_FADE_MS = 250;

const listeners = new Set<() => void>();
let current: ThemePreference | null = null;
let fadeTimer: number | undefined;

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

function matches(query: string): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

export function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "system") return matches(DARK_QUERY) ? "dark" : "light";
  return preference;
}

/** Applies a theme to the document, fading colours briefly unless reduced motion is asked for. */
export function applyTheme(preference: ThemePreference, { animate = false } = {}): void {
  const root = document.documentElement;
  const next = resolveTheme(preference);
  if (root.dataset.theme === next) return;
  if (animate && !matches(REDUCED_MOTION_QUERY)) {
    root.classList.add("theme-changing");
    window.clearTimeout(fadeTimer);
    fadeTimer = window.setTimeout(() => {
      root.classList.remove("theme-changing");
    }, CHANGE_FADE_MS);
  }
  root.dataset.theme = next;
}

export function setThemePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, preference);
  } catch {
    // A preference only; the choice still applies until the page is reloaded.
  }
  current = preference;
  applyTheme(preference, { animate: true });
  for (const listener of listeners) listener();
}

function snapshot(): ThemePreference {
  current ??= readThemePreference();
  return current;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab changed the preference.
  const onStorage = (event: StorageEvent) => {
    if (event.key !== THEME_KEY && event.key !== null) return;
    current = readThemePreference();
    applyTheme(current, { animate: true });
    listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The stored preference and a setter; follows the system setting while "system" is chosen. */
export function useTheme(): [ThemePreference, (preference: ThemePreference) => void] {
  const preference = useSyncExternalStore(subscribe, snapshot, () => "system" as const);
  useEffect(() => {
    applyTheme(preference);
    if (preference !== "system" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(DARK_QUERY);
    const follow = () => {
      applyTheme("system", { animate: true });
    };
    media.addEventListener("change", follow);
    return () => {
      media.removeEventListener("change", follow);
    };
  }, [preference]);
  const set = useCallback((next: ThemePreference) => {
    setThemePreference(next);
  }, []);
  return [preference, set];
}
