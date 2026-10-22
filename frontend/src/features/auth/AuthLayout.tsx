import { CalendarCheck, ShieldCheck, UsersRound } from "lucide-react";
import { useId, type ReactNode } from "react";

const BENEFITS = [
  {
    Icon: CalendarCheck,
    title: "Attendance and leave in one place",
    text: "Clock in, request leave and follow approvals without paperwork.",
  },
  {
    Icon: UsersRound,
    title: "Employee records kept current",
    text: "Profiles, documents and history, with every change recorded.",
  },
  {
    Icon: ShieldCheck,
    title: "Access that fits each role",
    text: "Two-step sign-in for everyone, and each person sees only what their role allows.",
  },
] as const;

function BrandMark() {
  return (
    <span className="flex items-center gap-2.5">
      <span
        aria-hidden
        className="grid size-9 place-items-center rounded-[var(--radius-surface)] bg-brand-ink/12 text-base font-semibold text-brand-ink ring-1 ring-brand-ink/25"
      >
        S
      </span>
      <span className="text-sm font-semibold tracking-wide text-brand-ink">Sigvitas HRMS</span>
    </span>
  );
}

/** Rings and a dot grid behind the brand panel. Decoration only. */
function Decoration() {
  // Both the band and the panel draw this; one of them is hidden, so each needs its own ids.
  const id = useId();
  const dots = `${id}-dots`;
  const glow = `${id}-glow`;
  return (
    <svg aria-hidden className="pointer-events-none absolute inset-0 size-full" focusable="false">
      <defs>
        <pattern id={dots} width="22" height="22" patternUnits="userSpaceOnUse">
          <circle cx="2" cy="2" r="1.2" className="fill-brand-ink/10" />
        </pattern>
        <radialGradient id={glow} cx="85%" cy="10%" r="60%">
          <stop offset="0%" className="[stop-color:var(--color-brand-glow)]" stopOpacity="0.45" />
          <stop offset="100%" className="[stop-color:var(--color-brand-glow)]" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${glow})`} />
      <rect width="100%" height="100%" fill={`url(#${dots})`} />
      <circle cx="92%" cy="88%" r="180" fill="none" className="stroke-brand-ink/10" strokeWidth="1.5" />
      <circle cx="92%" cy="88%" r="280" fill="none" className="stroke-brand-ink/8" strokeWidth="1.5" />
      <circle cx="8%" cy="6%" r="120" fill="none" className="stroke-brand-ink/8" strokeWidth="1.5" />
    </svg>
  );
}

/** The frame for screens used before signing in: the form on one side, the brand on the other.
 * Below the large breakpoint the brand shrinks to a band above the form. */
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="grid min-h-screen bg-neutral-50 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <main id="main" className="flex min-w-0 flex-col">
        <div className="relative overflow-hidden bg-linear-to-br from-brand-950 via-brand-800 to-brand-600 px-4 py-5 sm:px-8 lg:hidden">
          <Decoration />
          <div className="relative">
            <BrandMark />
            <p className="mt-2 text-sm text-brand-ink-muted">
              Attendance, leave and employee records for Sigvitas.
            </p>
          </div>
        </div>
        <div className="flex flex-1 flex-col justify-center px-4 py-10 sm:px-8">
          <div className="mx-auto w-full max-w-md">
            <h1 className="mb-6 text-[1.75rem] leading-tight font-semibold">{title}</h1>
            {children}
          </div>
        </div>
        <p className="px-4 pb-6 text-center text-xs text-neutral-600 sm:px-8">
          For Sigvitas employees. Access is recorded.
        </p>
      </main>
      <aside
        aria-label="About Sigvitas HRMS"
        className="relative hidden overflow-hidden bg-linear-to-br from-brand-950 via-brand-800 to-brand-600 lg:flex lg:flex-col lg:justify-between lg:p-12 xl:p-16"
      >
        <Decoration />
        <div className="relative">
          <BrandMark />
        </div>
        <div className="relative max-w-lg">
          <p className="text-3xl leading-tight font-semibold text-brand-ink xl:text-4xl">
            Welcome to Sigvitas HRMS
          </p>
          <p className="mt-4 text-base text-brand-ink-muted">
            One place for your working day: attendance, leave, records and the people you work with.
          </p>
          <ul className="mt-10 flex flex-col gap-6">
            {BENEFITS.map(({ Icon, title: benefit, text }) => (
              <li key={benefit} className="flex gap-4">
                <span className="grid size-10 shrink-0 place-items-center rounded-[var(--radius-surface)] bg-brand-ink/10 ring-1 ring-brand-ink/20">
                  <Icon aria-hidden className="size-5 text-brand-ink" />
                </span>
                <span>
                  <span className="block font-semibold text-brand-ink">{benefit}</span>
                  <span className="block text-sm text-brand-ink-muted">{text}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-sm text-brand-ink-muted">
          Every sign-in is confirmed with your authenticator app.
        </p>
      </aside>
    </div>
  );
}
