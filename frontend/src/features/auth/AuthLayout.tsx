import type { ReactNode } from "react";

/** The frame for screens used before signing in. */
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main id="main" className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-10">
      <p className="mb-6 text-sm font-semibold text-neutral-600">Sigvitas HRMS</p>
      <h1 className="mb-6 text-[1.375rem] font-semibold">{title}</h1>
      {children}
    </main>
  );
}
