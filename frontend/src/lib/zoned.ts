/**
 * Wall-clock times in a named time zone. Attendance times are entered and shown in the
 * employee's location zone, which need not be the viewer's own (formatting only: the API
 * decides what a time means).
 */

function zoneParts(instant: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((p) => p.type === type)?.value ?? 0);
  return Date.UTC(
    value("year"),
    value("month") - 1,
    value("day"),
    value("hour"),
    value("minute"),
    value("second"),
  );
}

/** The instant (ISO, UTC) at which the clock in `timeZone` shows `date` (`YYYY-MM-DD`) and
 * `time` (`HH:MM`). */
export function zonedToIso(date: string, time: string, timeZone: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  const wall = Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1, hour ?? 0, minute ?? 0);
  // Two passes settle the offset, including around a daylight-saving change.
  let guess = wall - (zoneParts(wall, timeZone) - wall);
  guess = wall - (zoneParts(guess, timeZone) - guess);
  return new Date(guess).toISOString();
}

/** `HH:MM` of an instant on the clock in `timeZone`. */
export function zonedTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(iso));
}

/** `YYYY-MM-DD` of an instant on the calendar in `timeZone`. */
export function zonedDate(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}
