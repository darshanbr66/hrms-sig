import type { ReactNode } from "react";

import { DayLine, LeavePath, Wordmark } from "../brand/BrandMarks";

/** What the HRMS does today, in the order of a working day. Nothing here is invented: each line
 * names a built module (docs/module-acceptance.md). */
const CONTENTS = [
  { number: "01", title: "Attendance", text: "Clock in and out, breaks, shifts and corrections." },
  { number: "02", title: "Leave", text: "Requests, approvals, balances and the team calendar." },
  { number: "03", title: "People", text: "Records, documents and the company directory." },
] as const;

/** The frame for screens used before signing in.
 *
 * Wide screens: an editorial page about the HRMS on the left and the form in a quiet column on
 * the right. The form comes first in the document, so keyboard and screen reader users reach it
 * before the introduction. Narrow screens: a short masthead (name, one line, the working day)
 * above the form, and nothing after it. */
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="grid min-h-screen bg-neutral-0 lg:grid-cols-[minmax(0,7fr)_minmax(26rem,5fr)]">
      <main id="main" className="flex min-w-0 flex-col lg:col-start-2 lg:row-start-1">
        <div className="border-b border-paper-line bg-paper px-4 pt-5 pb-6 sm:px-8 lg:hidden">
          <Wordmark />
          <p className="mt-5 max-w-md font-display text-[1.75rem] leading-[1.15] text-paper-ink sm:text-[2rem]">
            Every working day, <em className="block text-signal">accounted for.</em>
          </p>
          <DayLine compact />
        </div>
        <div className="flex flex-1 flex-col px-4 py-8 sm:px-8 sm:py-12 lg:justify-center lg:px-12 lg:py-10">
          <div className="mx-auto w-full max-w-sm">
            <h1 className="mb-6 font-display text-[2rem] leading-tight font-semibold text-neutral-900">
              {title}
            </h1>
            {children}
          </div>
        </div>
        <p className="px-4 pb-6 text-center text-xs text-neutral-600 sm:px-8">
          For Sigvitas employees. Access is recorded.
        </p>
      </main>
      <aside
        aria-label="About Sigvitas HRMS"
        className="relative hidden min-w-0 flex-col justify-between border-r border-paper-line bg-paper px-12 py-10 lg:col-start-1 lg:row-start-1 lg:flex xl:px-20 xl:py-14"
      >
        <Wordmark />
        <div className="max-w-2xl py-12">
          <p className="font-mono text-xs tracking-[0.18em] text-paper-ink-soft uppercase">
            Human resources at Sigvitas
          </p>
          <p className="mt-6 font-display text-[3.25rem] leading-[1.05] tracking-tight text-paper-ink xl:text-[4.25rem]">
            Every working day, <em className="text-signal">accounted for.</em>
          </p>
          <p className="mt-6 max-w-lg text-lg leading-relaxed text-paper-ink-soft">
            Your hours, your leave and your record in one place. Managers approve, HR keeps the details right,
            and every change leaves a trail.
          </p>
          <div className="mt-12 flex max-w-xl flex-col gap-8">
            <DayLine />
            <LeavePath />
          </div>
        </div>
        <div>
          <ol className="grid gap-6 border-t border-paper-line pt-6 xl:grid-cols-3">
            {CONTENTS.map((item) => (
              <li key={item.number} className="flex gap-3 xl:flex-col xl:gap-1">
                <span className="font-mono text-xs text-signal">{item.number}</span>
                <span>
                  <span className="block font-display text-lg text-paper-ink">{item.title}</span>
                  <span className="block text-sm text-paper-ink-soft">{item.text}</span>
                </span>
              </li>
            ))}
          </ol>
          <p className="mt-8 text-xs text-paper-ink-soft">
            Two-step sign-in on every account. Every change is recorded.
          </p>
        </div>
      </aside>
    </div>
  );
}
