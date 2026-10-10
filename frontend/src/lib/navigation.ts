/**
 * Post-sign-in redirects accept only paths inside this app (docs/security-architecture.md §5,
 * open redirect): the home page, or a relative path starting with one of the app's route
 * prefixes.
 */
const APP_PREFIXES = [
  "/account/",
  "/settings",
  "/admin/",
  "/attendance",
  "/team/",
  "/holidays",
  "/profile",
  "/directory",
  "/employees",
  "/organization",
  "/setup/",
];
export const HOME = "/";

export function safeNext(next: string | null | undefined): string {
  if (!next?.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return HOME;
  }
  return next === HOME || APP_PREFIXES.some((prefix) => next.startsWith(prefix)) ? next : HOME;
}
