/** Dates and times are formatted in the user's time zone (docs/api-architecture.md §3). */
const DATE_TIME = new Intl.DateTimeFormat("en-IN", {
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
