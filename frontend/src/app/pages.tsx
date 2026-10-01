import { Link } from "react-router";

import { PageHeader } from "../design-system/PageHeader";

export function AccessDenied() {
  return (
    <div>
      <PageHeader title="You don't have access to this page" />
      <p className="text-sm text-neutral-700">
        If you need it for your work, ask your HR team.{" "}
        <Link className="text-accent-700 underline" to="/account/security">
          Go to your account
        </Link>
      </p>
    </div>
  );
}

export function NotFound() {
  return (
    <main id="main" className="mx-auto max-w-[720px] p-6">
      <PageHeader title="Page not found" />
      <p className="text-sm text-neutral-700">
        Check the address, or{" "}
        <Link className="text-accent-700 underline" to="/account/security">
          go to your account
        </Link>
        .
      </p>
    </main>
  );
}
