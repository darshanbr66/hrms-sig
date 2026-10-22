/**
 * The Sigvitas HRMS marks shared by the public home page and the sign-in screens: the wordmark,
 * the working day drawn as the four attendance events, and the path of a leave request. All are
 * drawn from the paper tokens in styles.css and show no data.
 */
// The four attendance events, by the names the product uses, placed on a 3 : 1 : 3 line.
const DAY = [
  { label: "Clock in", at: 0, align: "" },
  { label: "Break", at: 3 / 7, align: "-translate-x-1/2", inner: true },
  { label: "Back", at: 4 / 7, align: "-translate-x-1/2", inner: true },
  { label: "Clock out", at: 1, align: "-translate-x-full" },
] as const;

export function Wordmark() {
  return (
    <span className="flex items-baseline gap-2 text-paper-ink">
      <span className="font-display text-xl font-semibold tracking-tight">Sigvitas</span>
      <span className="rounded-sm border border-paper-ink/40 px-1 font-mono text-[0.6875rem] tracking-[0.18em]">
        HRMS
      </span>
    </span>
  );
}

/** A working day drawn as a line: work in ink, the break in the accent. Decoration only.
 * `endsOnly` labels just clock in and clock out, for narrow places. */
export function DayLine({ compact = false, endsOnly = false }: { compact?: boolean; endsOnly?: boolean }) {
  return (
    <div aria-hidden className={compact ? "mt-5" : ""}>
      <div className="relative grid grid-cols-[3fr_1fr_3fr] items-center">
        <span className="animate-draw h-0.5 origin-left bg-paper-ink" />
        <span className="animate-draw h-0 origin-left border-t-2 border-dashed border-signal [animation-delay:150ms]" />
        <span className="animate-draw h-0.5 origin-left bg-paper-ink [animation-delay:300ms]" />
        {DAY.map(({ label, at }) => (
          <span
            key={label}
            style={{ left: `${String(at * 100)}%` }}
            className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-paper-ink bg-paper"
          />
        ))}
      </div>
      <div className="relative mt-3 h-4 font-mono text-[0.6875rem] tracking-wide text-paper-ink-soft uppercase">
        {DAY.map((point) => (
          <span
            key={point.label}
            style={{ left: `${String(point.at * 100)}%` }}
            className={`absolute whitespace-nowrap ${point.align} ${
              "inner" in point ? (endsOnly ? "hidden" : compact ? "hidden sm:inline" : "") : ""
            }`}
          >
            {point.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/** How a leave request travels. Decoration only. */
export function LeavePath() {
  return (
    <ol aria-hidden className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-paper-ink">
      {["Request", "Manager approves", "Balance updated"].map((step, index) => (
        <li key={step} className="flex items-center gap-3">
          {index > 0 ? <span className="text-signal">→</span> : null}
          <span className="rounded-full border border-paper-line px-3 py-1">{step}</span>
        </li>
      ))}
    </ol>
  );
}
