/**
 * The application shell (ui-ux-guidelines.md §3): skip link, a sidebar that collapses to
 * icons on desktop and becomes a drawer on smaller screens, a top bar with the page title,
 * directory search and the account menu, and a bottom bar on phones.
 *
 * Navigation lists only what the user may open, each item with the permissions it needs.
 * While an elevation or break-glass applies, a banner says which role and until when. Showing
 * or hiding anything here is cosmetic: the API decides every request.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronsLeft,
  ChevronsRight,
  Ellipsis,
  LayoutDashboard,
  LogOut,
  Menu as MenuIcon,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  Button as AriaButton,
  Dialog as AriaDialog,
  Header,
  Menu,
  MenuItem,
  MenuTrigger,
  Modal,
  ModalOverlay,
  Popover,
  RouterProvider as AriaRouterProvider,
  MenuSection,
  Separator,
} from "react-aria-components";
import { Link, NavLink, Outlet, useHref, useLocation, useNavigate } from "react-router";

import { Avatar } from "../design-system/Avatar";
import { Banner } from "../design-system/Banner";
import { SearchField } from "../design-system/Fields";
import { client, unwrap } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { canSee, titleFor, visibleNavGroups, type NavGroup } from "./navigation";
import { ELEVATION_KEY, hasPermission, useMe, type Me } from "./session";

export { visibleNavItems } from "./navigation";

const COLLAPSED_KEY = "sv-sidebar-collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(value: boolean): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, value ? "1" : "0");
  } catch {
    // A preference only; the sidebar works without it.
  }
}

export function AppShell() {
  const navigate = useNavigate();
  return (
    // React Aria links (menu items) navigate inside the app instead of reloading it.
    <AriaRouterProvider navigate={(path) => void navigate(path)} useHref={useHref}>
      <Shell />
    </AriaRouterProvider>
  );
}

function Shell() {
  const me = useMe();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const groups = visibleNavGroups(me.data);
  const title = titleFor(location.pathname);

  useEffect(() => {
    document.title = `${title} · Sigvitas HRMS`;
  }, [title]);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      writeCollapsed(!value);
      return !value;
    });
  };

  const showNavigation = me.data?.session_scope === "full";

  return (
    <div className="min-h-screen bg-neutral-50">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-neutral-0 focus:px-3 focus:py-2"
      >
        Skip to main content
      </a>
      {showNavigation ? (
        <aside
          className={`fixed inset-y-0 left-0 z-30 hidden flex-col border-r border-neutral-200 bg-neutral-0 lg:flex ${
            collapsed ? "w-[var(--width-sidebar-collapsed)]" : "w-[var(--width-sidebar)]"
          }`}
        >
          <Brand collapsed={collapsed} />
          <Navigation groups={groups} collapsed={collapsed} label="Main" />
          <div className="border-t border-neutral-200 p-2">
            <AriaButton
              onPress={toggleCollapsed}
              aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
              className="flex w-full items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-600 hover:bg-neutral-100"
            >
              {collapsed ? (
                <ChevronsRight aria-hidden className="size-4" />
              ) : (
                <>
                  <ChevronsLeft aria-hidden className="size-4" />
                  Collapse
                </>
              )}
            </AriaButton>
          </div>
        </aside>
      ) : null}

      <div
        className={
          showNavigation
            ? collapsed
              ? "lg:pl-[var(--width-sidebar-collapsed)]"
              : "lg:pl-[var(--width-sidebar)]"
            : ""
        }
      >
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-neutral-200 bg-neutral-0 px-4">
          {showNavigation ? (
            <AriaButton
              aria-label="Open navigation"
              onPress={() => {
                setDrawerOpen(true);
              }}
              className="rounded-[var(--radius-control)] p-2 text-neutral-700 hover:bg-neutral-100 lg:hidden"
            >
              <MenuIcon aria-hidden className="size-5" />
            </AriaButton>
          ) : (
            <Brand collapsed={false} />
          )}
          <p className="min-w-0 flex-1 truncate text-sm font-semibold text-neutral-800" aria-hidden>
            {showNavigation ? title : null}
          </p>
          {showNavigation ? <HeaderSearch me={me.data} /> : null}
          {me.data ? <AccountMenu me={me.data} /> : null}
        </header>

        <main id="main" className="mx-auto w-full max-w-[1280px] px-4 pb-24 pt-6 sm:px-6 lg:pb-10">
          <ElevationBanner me={me.data} />
          <Outlet />
        </main>
      </div>

      {showNavigation ? (
        <>
          <BottomBar
            me={me.data}
            onMore={() => {
              setDrawerOpen(true);
            }}
          />
          <ModalOverlay
            isOpen={drawerOpen}
            onOpenChange={setDrawerOpen}
            isDismissable
            className="fixed inset-0 z-40 bg-neutral-900/40 lg:hidden"
          >
            <Modal className="fixed inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-neutral-0 shadow-[var(--shadow-float)]">
              <AriaDialog aria-label="Navigation" className="flex h-full flex-col outline-none">
                <div className="flex items-center justify-between pr-2">
                  <Brand collapsed={false} />
                  <AriaButton
                    aria-label="Close navigation"
                    onPress={() => {
                      setDrawerOpen(false);
                    }}
                    className="rounded-[var(--radius-control)] p-2 text-neutral-700 hover:bg-neutral-100"
                  >
                    <X aria-hidden className="size-5" />
                  </AriaButton>
                </div>
                <Navigation
                  groups={groups}
                  collapsed={false}
                  label="Main (menu)"
                  onNavigate={() => {
                    setDrawerOpen(false);
                  }}
                />
              </AriaDialog>
            </Modal>
          </ModalOverlay>
        </>
      ) : null}
    </div>
  );
}

function Brand({ collapsed }: { collapsed: boolean }) {
  return (
    <Link
      to="/"
      className="flex h-14 shrink-0 items-center gap-2 px-4 font-semibold text-neutral-900"
      aria-label="Sigvitas HRMS, dashboard"
    >
      <span className="flex size-7 items-center justify-center rounded-[var(--radius-control)] bg-accent-700 text-neutral-0">
        <ShieldCheck aria-hidden className="size-4" />
      </span>
      {collapsed ? null : <span>Sigvitas HRMS</span>}
    </Link>
  );
}

function Navigation({
  groups,
  collapsed,
  label,
  onNavigate,
}: {
  groups: NavGroup[];
  collapsed: boolean;
  label: string;
  onNavigate?: () => void;
}) {
  return (
    <nav aria-label={label} className="flex-1 overflow-y-auto px-2 py-3">
      {groups.map((group) => (
        <div key={group.label} className="mb-4">
          {collapsed ? (
            <hr className="mx-2 mb-2 border-neutral-200" />
          ) : (
            <p className="px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              {group.label}
            </p>
          )}
          <ul className="flex flex-col gap-0.5">
            {group.items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end ?? false}
                  title={collapsed ? item.label : undefined}
                  {...(onNavigate ? { onClick: onNavigate } : {})}
                  className={({ isActive }) =>
                    `flex items-center gap-3 rounded-[var(--radius-control)] px-3 py-2 text-sm ${
                      isActive
                        ? "bg-accent-50 font-semibold text-accent-700"
                        : "text-neutral-700 hover:bg-neutral-100 hover:text-neutral-900"
                    } ${collapsed ? "justify-center px-0" : ""}`
                  }
                >
                  <item.icon aria-hidden className="size-4 shrink-0" />
                  <span className={collapsed ? "sr-only" : ""}>{item.label}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** Directory search for everyone who may use the directory; HR without it searches records. */
function HeaderSearch({ me }: { me: Me | undefined | null }) {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const target = hasPermission(me, "employee.directory.read")
    ? "/directory"
    : canSee(me, { anyOf: ["employee.profile.read.team", "employee.profile.read.all"] })
      ? "/employees"
      : null;
  if (target === null) return null;
  return (
    <div className="hidden w-64 md:block">
      <SearchField
        label={target === "/directory" ? "Search the directory" : "Search employees"}
        placeholder={target === "/directory" ? "Search people" : "Search employees"}
        value={text}
        onChange={setText}
        onSubmit={(value) => {
          const query = value.trim();
          void navigate(query ? `${target}?q=${encodeURIComponent(query)}` : target);
          setText("");
        }}
      />
    </div>
  );
}

function AccountMenu({ me }: { me: Me }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const signOut = async () => {
    try {
      await unwrap(client.POST("/api/v1/auth/logout"));
    } finally {
      queryClient.clear();
      void navigate("/sign-in", { replace: true });
    }
  };
  const fullSession = me.session_scope === "full";
  return (
    <MenuTrigger>
      <AriaButton
        aria-label="Account menu"
        className="flex items-center gap-2 rounded-full p-0.5 hover:bg-neutral-100 sm:rounded-[var(--radius-control)] sm:px-2 sm:py-1"
      >
        <Avatar name={me.email} size="sm" />
        <span className="hidden max-w-48 truncate text-sm text-neutral-700 sm:inline">{me.email}</span>
      </AriaButton>
      <Popover
        placement="bottom end"
        className="w-64 rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-1 shadow-[var(--shadow-float)]"
      >
        <Menu
          aria-label="Account"
          className="outline-none"
          onAction={(key) => {
            if (key === "sign-out") void signOut();
          }}
        >
          <MenuSection>
            <Header className="px-3 py-2">
              <p className="truncate text-sm font-semibold text-neutral-900">{me.email}</p>
              <p className="text-xs text-neutral-600">
                {me.roles.length > 0
                  ? me.roles.map((role) => role.replace(/_/g, " ")).join(", ")
                  : "No roles"}
              </p>
            </Header>
          </MenuSection>
          <Separator className="my-1 border-t border-neutral-200" />
          {fullSession && hasPermission(me, "employee.profile.read.self") ? (
            <MenuItem
              id="profile"
              href="/profile"
              className="flex cursor-default items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-800 outline-none focus:bg-neutral-100"
            >
              <UserRound aria-hidden className="size-4" />
              My profile
            </MenuItem>
          ) : null}
          {fullSession ? (
            <MenuItem
              id="security"
              href="/account/security"
              className="flex cursor-default items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-800 outline-none focus:bg-neutral-100"
            >
              <ShieldCheck aria-hidden className="size-4" />
              Account security
            </MenuItem>
          ) : null}
          <MenuItem
            id="sign-out"
            className="flex cursor-default items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm text-neutral-800 outline-none focus:bg-neutral-100"
          >
            <LogOut aria-hidden className="size-4" />
            Sign out
          </MenuItem>
        </Menu>
      </Popover>
    </MenuTrigger>
  );
}

const COLUMNS: Record<number, string> = { 2: "grid-cols-2", 3: "grid-cols-3", 4: "grid-cols-4" };

/** Phones: the most used destinations within thumb reach, the rest under "More". */
function BottomBar({ me, onMore }: { me: Me | null | undefined; onMore: () => void }) {
  const items = [
    { to: "/", label: "Home", icon: LayoutDashboard, anyOf: [] as string[], end: true },
    ...visibleNavGroups(me)
      .flatMap((group) => group.items)
      .filter((item) => ["/attendance", "/directory", "/employees"].includes(item.to))
      .slice(0, 2),
  ];
  return (
    <nav
      aria-label="Quick"
      className={`fixed inset-x-0 bottom-0 z-30 grid border-t border-neutral-200 bg-neutral-0 lg:hidden ${
        COLUMNS[items.length + 1] ?? "grid-cols-4"
      }`}
    >
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end ?? false}
          className={({ isActive }) =>
            `flex min-h-14 flex-col items-center justify-center gap-0.5 text-xs ${
              isActive ? "font-semibold text-accent-700" : "text-neutral-600"
            }`
          }
        >
          <item.icon aria-hidden className="size-5" />
          {item.label}
        </NavLink>
      ))}
      <AriaButton
        onPress={onMore}
        className="flex min-h-14 flex-col items-center justify-center gap-0.5 text-xs text-neutral-600"
      >
        <Ellipsis aria-hidden className="size-5" />
        More
      </AriaButton>
    </nav>
  );
}

/** Temporary access is always visible while it lasts (docs/authorization-model.md §4.3). */
function ElevationBanner({ me }: { me: Me | null | undefined }) {
  const fullSession = me?.session_scope === "full";
  const elevation = useQuery({
    queryKey: ELEVATION_KEY,
    queryFn: () => unwrap(client.GET("/api/v1/me/elevation")),
    enabled: fullSession,
    staleTime: 30_000,
  });
  const current = fullSession ? elevation.data?.elevation : null;
  if (!current?.ends_at) return null;
  const kind = current.kind === "break_glass" ? "break-glass" : "temporary";
  return (
    <div className="mb-4">
      <Banner tone="warning">
        You have {kind} {current.role_key} access until {formatDateTime(current.ends_at)}. Everything you do
        is recorded against it.{" "}
        {hasPermission(me, "role.read") ? (
          <Link className="underline" to="/admin/access-requests">
            See access requests
          </Link>
        ) : null}
      </Banner>
    </div>
  );
}
