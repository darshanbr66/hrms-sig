/**
 * Formatting only (CLAUDE.md rule 14): values arrive from the API already calculated.
 *
 * Timestamps are formatted in the viewer's time zone unless a zone is given (attendance shows
 * the employee's location zone). Calendar dates (`YYYY-MM-DD`) are dates, not instants, so
 * they are formatted without any time zone conversion.
 */
const LOCALE = "en-IN";

const DATE_TIME = new Intl.DateTimeFormat(LOCALE, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDateTime(value: string): string {
  return DATE_TIME.format(new Date(value));
}

/** "Tue, 30 Sep 2026" for a calendar date. */
export function formatDate(value: string, options: { weekday?: boolean; year?: boolean } = {}): string {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1));
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone: "UTC",
    day: "numeric",
    month: "short",
    ...(options.weekday === false ? {} : { weekday: "short" }),
    ...(options.year === false ? {} : { year: "numeric" }),
  }).format(date);
}

/** "09:42" for an instant, in the given IANA zone or the viewer's own. */
export function formatTime(value: string, timeZone?: string): string {
  return new Intl.DateTimeFormat(LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    ...(timeZone ? { timeZone } : {}),
  }).format(new Date(value));
}

/** "7h 42m" from whole seconds. */
export function formatDuration(seconds: number): string {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest}m`;
  return `${hours}h ${String(rest).padStart(2, "0")}m`;
}

const RELATIVE = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" });

/** "2 hours ago" for recent activity; callers show the absolute time on hover. */
export function formatRelative(value: string, now: Date = new Date()): string {
  const seconds = Math.round((new Date(value).getTime() - now.getTime()) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return RELATIVE.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

/** Today's calendar date in the viewer's time zone, as `YYYY-MM-DD`. */
export function localIsoDate(date: Date = new Date()): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** A calendar date some days after another, as `YYYY-MM-DD`. */
export function addDays(isoDate: string, days: number): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, (day ?? 1) + days));
  return date.toISOString().slice(0, 10);
}

/** "Wednesday, 7 October 2026" for the page greeting. */
export function formatLongToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat(LOCALE, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(now);
}

export function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** "full_time" -> "Full time". */
export function humanize(value: string): string {
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
