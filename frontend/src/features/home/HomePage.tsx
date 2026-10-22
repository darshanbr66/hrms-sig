/**
 * The public home page at `/`. It is static: it makes no API request, so it looks the same
 * whether the API is awake, starting or down. Signing in starts at `/sign-in`, whose readiness
 * check wakes the API (lib/readiness.ts). Every line describes something the HRMS does today
 * (docs/module-acceptance.md); the product view is an illustration and says so.
 */
import { ArrowRight, Check } from "lucide-react";
import { Link } from "react-router";

import { ButtonLink } from "../../design-system/Button";
import { DayLine, LeavePath, Wordmark } from "../brand/BrandMarks";

const AREAS = [
  {
    title: "Employee records",
    text: "Profiles, job history and status, documents and the company directory. A change adds to the history; nothing is overwritten.",
  },
  {
    title: "Attendance",
    text: "Clock in and out with breaks, shifts and attendance policies. Corrections go to an approver and are kept beside the original events.",
  },
  {
    title: "Leave",
    text: "Leave types and policies set by HR, requests and approvals, balances, holiday calendars and a team calendar.",
  },
  {
    title: "Organization",
    text: "Departments, designations, locations and reporting lines, and roles that decide who sees what.",
  },
] as const;

const SAFEGUARDS = [
  "Two-step sign-in on every account",
  "Each person sees only what their role allows",
  "Every change is recorded in the audit log",
] as const;

function SignInLink({ size = "md" }: { size?: "sm" | "md" }) {
  return (
    <ButtonLink to="/sign-in" size={size}>
      Employee sign in
      <ArrowRight aria-hidden className="size-4" />
    </ButtonLink>
  );
}

/** A quiet sketch of the signed-in home: grey bars stand in for text, so nothing reads as data. */
function ProductSketch() {
  const bar = "h-2 rounded-full bg-neutral-200";
  return (
    <figure className="min-w-0">
      <div
        aria-hidden
        className="overflow-hidden rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 shadow-[var(--shadow-float)]"
      >
        <div className="flex items-center gap-1.5 border-b border-neutral-200 px-4 py-3">
          {[0, 1, 2].map((dot) => (
            <span key={dot} className="size-2 rounded-full bg-neutral-300" />
          ))}
          <span className={`ml-3 w-24 ${bar}`} />
        </div>
        <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] sm:grid-cols-[7rem_minmax(0,1fr)]">
          <div className="flex flex-col gap-3 border-r border-neutral-200 p-4">
            <span className="h-2 w-full rounded-full bg-accent-700" />
            {[80, 65, 90, 55, 70].map((width) => (
              <span key={width} className={bar} style={{ width: `${String(width)}%` }} />
            ))}
          </div>
          <div className="flex min-w-0 flex-col gap-5 p-4 sm:p-6">
            <div className="flex items-center justify-between gap-3">
              <span className="font-display text-lg text-neutral-900">Today</span>
              <span className="rounded-[var(--radius-control)] bg-accent-700 px-3 py-1.5 text-xs font-semibold text-neutral-0">
                Clock in
              </span>
            </div>
            <div className="rounded-[var(--radius-control)] bg-paper px-4 pt-4 pb-2">
              <DayLine compact endsOnly />
            </div>
            <div className="flex flex-col gap-2 border-t border-neutral-200 pt-4">
              <span className="text-xs font-semibold text-neutral-600">Leave request</span>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className={`w-32 ${bar}`} />
                <span className="rounded-full bg-warning-50 px-2.5 py-0.5 text-xs font-semibold text-warning-700">
                  Waiting for approval
                </span>
              </div>
            </div>
            <div className="hidden flex-col items-center gap-2 border-t border-neutral-200 pt-4 sm:flex">
              <span className="rounded-[var(--radius-control)] border border-neutral-300 px-3 py-1 text-xs text-neutral-700">
                Department
              </span>
              <span className="h-3 w-px bg-neutral-300" />
              <div className="flex gap-3">
                {["Manager", "You"].map((node) => (
                  <span
                    key={node}
                    className={`rounded-[var(--radius-control)] border px-3 py-1 text-xs ${
                      node === "You" ? "border-signal text-signal" : "border-neutral-300 text-neutral-700"
                    }`}
                  >
                    {node}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
      <figcaption className="mt-3 text-xs text-paper-ink-soft">
        An illustration of the signed-in home, not live data.
      </figcaption>
    </figure>
  );
}

export function HomePage() {
  return (
    <div className="min-h-screen bg-paper text-paper-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-10 focus:rounded-[var(--radius-control)] focus:bg-neutral-0 focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>
      <header className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-5 sm:px-8">
        <Wordmark />
        <SignInLink size="sm" />
      </header>

      <main id="main">
        <section
          aria-labelledby="home-title"
          className="mx-auto grid max-w-6xl gap-12 px-4 pt-10 pb-16 sm:px-8 lg:grid-cols-[minmax(0,6fr)_minmax(0,5fr)] lg:items-center lg:gap-16 lg:pt-16 lg:pb-24"
        >
          <div>
            <p className="font-mono text-xs tracking-[0.18em] text-paper-ink-soft uppercase">Sigvitas HRMS</p>
            <h1
              id="home-title"
              className="mt-5 font-display text-[2.75rem] leading-[1.02] tracking-tight sm:text-[4rem] xl:text-[4.75rem]"
            >
              People first.
              <em className="block text-signal">Work, in order.</em>
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-paper-ink-soft">
              Employee records, attendance and leave for everyone at Sigvitas, with the approvals and history
              HR relies on kept in one place.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3">
              <SignInLink />
              <p className="text-sm text-paper-ink-soft">Invited by HR? Open the link in your email.</p>
            </div>
            <div className="mt-12 hidden max-w-md lg:block">
              <LeavePath />
            </div>
          </div>
          <ProductSketch />
        </section>

        <section aria-labelledby="areas-title" className="border-t border-paper-line">
          <div className="mx-auto max-w-6xl px-4 py-16 sm:px-8 lg:py-24">
            <div className="grid gap-10 lg:grid-cols-[minmax(0,4fr)_minmax(0,8fr)] lg:gap-16">
              <div>
                <h2 id="areas-title" className="font-display text-3xl leading-tight sm:text-4xl">
                  What the HRMS looks after
                </h2>
                <p className="mt-4 max-w-sm text-paper-ink-soft">
                  One system for the working day, from the first clock-in to the records HR keeps.
                </p>
              </div>
              <ol className="border-t border-paper-ink/80">
                {AREAS.map((area, index) => (
                  <li
                    key={area.title}
                    className="grid gap-2 border-b border-paper-line py-6 sm:grid-cols-[3rem_minmax(0,14rem)_minmax(0,1fr)] sm:gap-6"
                  >
                    <span className="font-mono text-xs text-signal sm:pt-1.5">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <h3 className="font-display text-xl">{area.title}</h3>
                    <p className="text-paper-ink-soft">{area.text}</p>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>

        <section aria-labelledby="care-title" className="bg-paper-ink text-paper">
          <div className="mx-auto grid max-w-6xl gap-8 px-4 py-14 sm:px-8 lg:grid-cols-[minmax(0,4fr)_minmax(0,8fr)] lg:gap-16">
            <h2 id="care-title" className="font-display text-2xl leading-tight sm:text-3xl">
              Personal data, handled with care
            </h2>
            <ul className="grid gap-4 sm:grid-cols-3">
              {SAFEGUARDS.map((item) => (
                <li key={item} className="flex gap-2 text-sm">
                  <Check aria-hidden className="mt-0.5 size-4 shrink-0" />
                  {item}
                </li>
              ))}
            </ul>
          </div>
        </section>
      </main>

      <footer className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm text-paper-ink-soft sm:px-8">
        <span className="flex flex-wrap items-baseline gap-3">
          <Wordmark />
          <span>For Sigvitas employees.</span>
        </span>
        <Link className="text-paper-ink underline underline-offset-4" to="/sign-in">
          Employee sign in
        </Link>
      </footer>
    </div>
  );
}
