/**
 * The application shell: skip link, header, navigation and the account menu.
 *
 * Navigation lists only what the user may open (ui-ux-guidelines.md §2). In this checkpoint
 * the only screen is the user's own account; later checkpoints add items with the permission
 * each needs. Showing or hiding an item is cosmetic: the API decides every request.
 */
import { useQueryClient } from "@tanstack/react-query";
import { LogOut, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { Button } from "../design-system/Button";
import { client, unwrap } from "../lib/api";
import { hasPermission, useMe, type Me } from "./session";

interface NavItem {
  to: string;
  label: string;
  permission: string | null;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/account/security", label: "Account security", permission: null },
];

export function visibleNavItems(me: Me | null | undefined): NavItem[] {
  return NAV_ITEMS.filter((item) => item.permission === null || hasPermission(me, item.permission));
}

export function AppShell() {
  const me = useMe();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [signingOut, setSigningOut] = useState(false);

  const signOut = async () => {
    setSigningOut(true);
    try {
      await unwrap(client.POST("/api/v1/auth/logout"));
    } finally {
      queryClient.clear();
      setSigningOut(false);
      void navigate("/sign-in", { replace: true });
    }
  };

  return (
    <div className="min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50"
      >
        Skip to main content
      </a>
      <header className="flex items-center justify-between gap-4 border-b border-neutral-200 bg-neutral-0 px-4 py-3">
        <span className="inline-flex items-center gap-2 font-semibold">
          <ShieldCheck aria-hidden className="size-5 text-accent-700" />
          Sigvitas HRMS
        </span>
        <div className="flex items-center gap-3 text-sm">
          {me.data ? <span className="hidden text-neutral-600 sm:inline">{me.data.email}</span> : null}
          <Button variant="secondary" onPress={() => void signOut()} isPending={signingOut}>
            <LogOut aria-hidden className="size-4" />
            Sign out
          </Button>
        </div>
      </header>
      <div className="mx-auto flex max-w-[1280px] flex-col gap-6 px-4 py-6 md:flex-row">
        <nav aria-label="Main" className="md:w-60 md:shrink-0">
          <ul className="flex gap-1 md:flex-col">
            {visibleNavItems(me.data).map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  className={({ isActive }) =>
                    `block rounded-[var(--radius-control)] px-3 py-2 text-sm ${
                      isActive
                        ? "bg-accent-50 font-semibold text-accent-700"
                        : "text-neutral-700 hover:bg-neutral-100"
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <main id="main" className="min-w-0 flex-1 md:max-w-[720px]">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
