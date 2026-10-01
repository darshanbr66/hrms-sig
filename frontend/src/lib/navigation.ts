/**
 * Post-sign-in redirects accept only paths inside this app (docs/security-architecture.md §5,
 * open redirect): a relative path starting with one of the app's route prefixes.
 */
const APP_PREFIXES = ["/account/"];
export const HOME = "/account/security";

export function safeNext(next: string | null | undefined): string {
  if (!next?.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return HOME;
  }
  return APP_PREFIXES.some((prefix) => next.startsWith(prefix)) ? next : HOME;
}
