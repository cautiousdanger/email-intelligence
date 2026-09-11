"use client";

import { useEffect, useLayoutEffect, useState, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession } from "next-auth/react";
import {
  LayoutDashboard,
  Mail,
  FolderOpen,
  List,
  PanelLeftClose,
  Users,
  Network,
  AlertCircle,
  UserCircle,
  MessageSquare,
  FolderKanban,
  ChevronRight,
  CalendarRange,
  ClipboardCheck,
  BookOpen,
  ShieldCheck,
  Trash2,
  ListTree,
  Globe2,
  GraduationCap,
  CalendarDays,
  type LucideIcon,
} from "lucide-react";
import { LenisScrollArea } from "@/components/lenis/lenis-scroll-area";
import { cn } from "@/lib/utils";
import { getApi } from "@/lib/api/client";
import { ME_UPDATED_EVENT } from "@/lib/me-sync-events";
import type { TeamOut } from "@/lib/types";

/** Layout (width, padding): keep short; labels use opacity-only to avoid max-width layout thrash. */
const SIDEBAR_LAYOUT_ANIM = "duration-200 ease-out motion-reduce:transition-none";
/** Compositor-only fades for text (no max-width transition). */
const SIDEBAR_LABEL_FADE = "duration-200 ease-out motion-reduce:transition-none";
const ADMIN_TEAMS_ICON_CLASS = "text-amber-500";
const ADMIN_PIPELINE_ICON_CLASS = "text-cyan-500";
const SKILLVAULT_URL = "http://172.16.200.30:3005/skillvault";

type SidebarNavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  iconClassName: string;
};

function formatSidebarRoleLabel(role: string, department: string | null, isAdmin: boolean): string {
  if (isAdmin) return "Admin";
  const roleLabel = (role ?? "Member").trim() || "Member";
  const team = (department ?? "").trim();
  return team ? `${roleLabel} · ${team}` : roleLabel;
}

function userInitials(displayName: string): string {
  const s = displayName.trim();
  if (!s) return "?";
  if (s.includes("@")) {
    const local = s.split("@")[0] ?? "";
    return (local.slice(0, 2) || "?").toUpperCase();
  }
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    const a = parts[0]?.[0] ?? "";
    const b = parts[1]?.[0] ?? "";
    return `${a}${b}`.toUpperCase() || "?";
  }
  return s.slice(0, 2).toUpperCase() || "?";
}

const navItemsTop: SidebarNavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, iconClassName: "text-violet-500" },
  { href: "/emails", label: "Mailbox", icon: Mail, iconClassName: "text-sky-500" },
  { href: "/daily-digest", label: "Daily digest", icon: CalendarDays, iconClassName: "text-amber-500" },
  { href: "/threads", label: "Threads", icon: MessageSquare, iconClassName: "text-emerald-500" },
];

const navItemsAfterDepartments: SidebarNavItem[] = [
  { href: "/escalations", label: "Escalations", icon: AlertCircle, iconClassName: "text-rose-500" },
  { href: "/leads", label: "Leads", icon: List, iconClassName: "text-orange-500" },
  { href: "/how-to-use", label: "How to use", icon: BookOpen, iconClassName: "text-indigo-500" },
];

/** Shown to org Managers only (admins use admin nav blocks below). Excludes admin-only routes. */
const managerAdminNavItems: SidebarNavItem[] = [
  { href: "/admin/my-projects", label: "Projects", icon: FolderKanban, iconClassName: "text-fuchsia-500" },
];

/** Tracker, Review, Escalations, Leads - rendered inside {@link AdminPipelineNavSection}. */
const adminPipelineDropdownItems: SidebarNavItem[] = [
  { href: "/admin/tracker", label: "Tracker", icon: CalendarRange, iconClassName: "text-cyan-500" },
  { href: "/admin/review", label: "Review", icon: ClipboardCheck, iconClassName: "text-lime-500" },
  { href: "/admin/escalations", label: "Escalations", icon: AlertCircle, iconClassName: "text-rose-500" },
  { href: "/admin/leads", label: "Leads", icon: List, iconClassName: "text-orange-500" },
];

/** Full admin: links before the pipeline dropdown. */
const adminNavBeforePipeline: SidebarNavItem[] = [
  { href: "/admin/team-leaders", label: "User management", icon: UserCircle, iconClassName: "text-pink-500" },
  { href: "/admin/team-projects", label: "Projects", icon: FolderKanban, iconClassName: "text-fuchsia-500" },
];

/** Full admin: links after the pipeline dropdown. */
const adminNavAfterPipeline: SidebarNavItem[] = [
  { href: "/admin/workflow", label: "Hierarchy", icon: Network, iconClassName: "text-teal-500" },
  { href: "/admin/deleted-mail", label: "Deleted mail", icon: Trash2, iconClassName: "text-red-500" },
  { href: "/admin/external-mail", label: "External mail", icon: Globe2, iconClassName: "text-blue-500" },
  { href: "/admin/approvals", label: "Approvals", icon: ShieldCheck, iconClassName: "text-green-500" },
  { href: "/admin/archive-projects", label: "Archive Projects", icon: FolderOpen, iconClassName: "text-yellow-500" },
];

const ADMIN_ONLY_HREFS = new Set([
  "/admin/team-leaders",
  "/admin/team-projects",
  "/admin/workflow",
  "/admin/approvals",
  "/admin/deleted-mail",
  "/admin/external-mail",
]);

const SIDEBAR_FLYOUT_Z = 500;

function computeSidebarFlyoutPosition(
  triggerEl: HTMLElement,
  minWidthPx: number,
  maxWidthPx: number
): { top: number; left: number; width: number; maxHeight: number } {
  const rect = triggerEl.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const gap = 4;
  const width = Math.min(maxWidthPx, Math.max(minWidthPx, Math.ceil(rect.width) + gap));
  let left = rect.right + gap;
  if (left + width > vw - 8) {
    left = Math.max(8, rect.left - width - gap);
  }
  let top = rect.top;
  const maxHeight = Math.min(vh * 0.7, vh - top - 8);
  if (top + maxHeight > vh - 8) {
    top = Math.max(8, vh - 8 - maxHeight);
  }
  return { top, left, width, maxHeight };
}

/** Renders next to collapsed sidebar icon; portaled so `overflow-auto` on nav does not clip. */
function CollapsedSidebarFlyout({
  open,
  triggerRef,
  onClose,
  minWidthPx,
  maxWidthPx,
  children,
}: {
  open: boolean;
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  minWidthPx: number;
  maxWidthPx: number;
  children: React.ReactNode;
}) {
  const [mounted, setMounted] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<{ top: number; left: number; width: number; maxHeight: number } | null>(null);

  useEffect(() => setMounted(true), []);

  useLayoutEffect(() => {
    if (!open) {
      setBox(null);
      return;
    }
    const upd = () => {
      const el = triggerRef.current;
      if (!el) return;
      setBox(computeSidebarFlyoutPosition(el, minWidthPx, maxWidthPx));
    };
    upd();
    window.addEventListener("resize", upd);
    window.addEventListener("scroll", upd, true);
    return () => {
      window.removeEventListener("resize", upd);
      window.removeEventListener("scroll", upd, true);
    };
  }, [open, triggerRef, minWidthPx, maxWidthPx]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t)) return;
      if (panelRef.current?.contains(t)) return;
      onClose();
    };
    const tid = window.setTimeout(() => document.addEventListener("mousedown", onDoc), 0);
    return () => {
      clearTimeout(tid);
      document.removeEventListener("mousedown", onDoc);
    };
  }, [open, onClose, triggerRef]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!mounted || !open || !box) return null;

  return createPortal(
    <div
      ref={panelRef}
      className="fixed overflow-hidden rounded-xl border border-border bg-panel py-1 shadow-lg"
      style={{
        top: box.top,
        left: box.left,
        width: box.width,
        maxHeight: box.maxHeight,
        zIndex: SIDEBAR_FLYOUT_Z,
      }}
      role="menu"
    >
      {children}
    </div>,
    document.body
  );
}

/** Admin/Manager: expandable Teams filtered by allowed department when provided. */
function AdminTeamsNavSection({
  collapsed,
  allowedDepartment,
  onNavigate,
}: {
  collapsed: boolean;
  allowedDepartment?: string | null;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );
  const underAdminTeams = pathname === "/admin/teams" || pathname.startsWith("/admin/teams/");
  const [accordionOpen, setAccordionOpen] = useState(underAdminTeams);
  const [flyoutOpen, setFlyoutOpen] = useState(false);
  const [teams, setTeams] = useState<TeamOut[]>([]);
  const teamsFlyoutTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (underAdminTeams) setAccordionOpen(true);
  }, [underAdminTeams]);

  useEffect(() => {
    if (status !== "authenticated") return;
    api.getTeams().then(setTeams).catch(() => setTeams([]));
  }, [status, api]);

  const sorted = useMemo(() => {
    const dep = (allowedDepartment ?? "").trim();
    const list = dep ? teams.filter((t) => (t.name ?? "").trim() === dep) : teams;
    return [...list].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  }, [teams, allowedDepartment]);

  const rowActive = underAdminTeams;

  if (collapsed) {
    return (
      <div className="relative flex justify-center px-1">
        <button
          ref={teamsFlyoutTriggerRef}
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setFlyoutOpen((o) => !o);
          }}
          className={cn(
            "flex w-full items-center justify-center rounded-lg p-2.5 transition-colors",
            rowActive || flyoutOpen
              ? "bg-foreground text-background"
              : "text-muted-foreground hover:bg-muted hover:text-foreground"
          )}
          aria-expanded={flyoutOpen}
          aria-haspopup="true"
          title="Teams (admin)"
        >
          <Users className={cn("h-5 w-5 shrink-0", ADMIN_TEAMS_ICON_CLASS)} />
        </button>
        <CollapsedSidebarFlyout
          open={flyoutOpen}
          triggerRef={teamsFlyoutTriggerRef}
          onClose={() => setFlyoutOpen(false)}
          minWidthPx={12 * 16}
          maxWidthPx={18 * 16}
        >
          <p className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Teams
          </p>
          <LenisScrollArea className="max-h-[70vh] min-h-0 max-w-full">
            <ul className="py-1">
              {!allowedDepartment && (
                <li>
                  <Link
                    href="/admin/teams"
                    role="menuitem"
                    onClick={() => {
                      setFlyoutOpen(false);
                      onNavigate?.();
                    }}
                    className={cn(
                      "flex items-center justify-between gap-3 px-3 py-2 text-sm",
                      pathname === "/admin/teams"
                        ? "bg-muted font-medium text-foreground"
                        : "text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                    )}
                  >
                    <span>All teams</span>
                  </Link>
                </li>
              )}
              {sorted.map((t) => {
                const href = `/admin/teams/${t.id}`;
                const active = pathname === href || pathname.startsWith(`${href}/`);
                return (
                  <li key={t.id}>
                    <Link
                      href={href}
                      role="menuitem"
                      onClick={() => {
                        setFlyoutOpen(false);
                        onNavigate?.();
                      }}
                      className={cn(
                        "flex items-center justify-between gap-3 px-3 py-2 text-sm",
                        active
                          ? "bg-muted font-medium text-foreground"
                          : "text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                      )}
                      title={t.name}
                    >
                      <span className="truncate">{t.name}</span>
                      <span className="shrink-0 tabular-nums text-xs text-muted-foreground">{t.memberCount}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </LenisScrollArea>
        </CollapsedSidebarFlyout>
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      <button
        type="button"
        onClick={() => setAccordionOpen((o) => !o)}
        className={cn(
          "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition-colors",
          rowActive
            ? "bg-foreground text-background"
            : "text-muted-foreground hover:bg-muted hover:text-foreground"
        )}
        aria-expanded={accordionOpen}
      >
        <Users className={cn("h-5 w-5 shrink-0", ADMIN_TEAMS_ICON_CLASS)} />
        <span className="min-w-0 flex-1">Teams</span>
        <ChevronRight
          className={cn("h-4 w-4 shrink-0 transition-transform duration-200", accordionOpen && "rotate-90")}
          aria-hidden
        />
      </button>
      {accordionOpen && (
        <ul
          className="ml-2 space-y-0.5 border-l border-border py-0.5 pl-2"
          role="list"
        >
          {!allowedDepartment && (
            <li>
              <Link
                href="/admin/teams"
                onClick={() => onNavigate?.()}
                className={cn(
                  "flex items-center justify-between gap-2 rounded-md px-2 py-2 text-sm transition-colors",
                  pathname === "/admin/teams"
                    ? "bg-accent font-medium text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted"
                )}
              >
                <span>All teams</span>
              </Link>
            </li>
          )}
          {sorted.map((t) => {
            const href = `/admin/teams/${t.id}`;
            const active = pathname === href || pathname.startsWith(`${href}/`);
            return (
              <li key={t.id}>
                <Link
                  href={href}
                  onClick={() => onNavigate?.()}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md px-2 py-2 text-sm transition-colors",
                    active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted"
                  )}
                  title={t.name}
                >
                  <span className="truncate">{t.name}</span>
                  <span className="shrink-0 tabular-nums text-xs opacity-80">{t.memberCount}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Admin/Manager: Tracker, Review, Escalations, and Leads under one expandable section. */
function AdminPipelineNavSection({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const underPipeline = useMemo(
    () =>
      adminPipelineDropdownItems.some(
        (item) => pathname === item.href || pathname.startsWith(`${item.href}/`)
      ),
    [pathname]
  );
  const [accordionOpen, setAccordionOpen] = useState(underPipeline);
  const [flyoutOpen, setFlyoutOpen] = useState(false);
  const flyoutTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (underPipeline) setAccordionOpen(true);
  }, [underPipeline]);

  const rowActive = underPipeline;

  if (collapsed) {
    return (
      <div className="relative flex justify-center px-1">
        <button
          ref={flyoutTriggerRef}
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setFlyoutOpen((o) => !o);
          }}
          className={cn(
            "flex w-full items-center justify-center rounded-lg p-2.5 transition-colors",
            rowActive || flyoutOpen
              ? "bg-foreground text-background"
              : "text-muted-foreground hover:bg-muted hover:text-foreground"
          )}
          aria-expanded={flyoutOpen}
          aria-haspopup="true"
          title="Tracking"
        >
          <ListTree className={cn("h-5 w-5 shrink-0", ADMIN_PIPELINE_ICON_CLASS)} aria-hidden />
        </button>
        <CollapsedSidebarFlyout
          open={flyoutOpen}
          triggerRef={flyoutTriggerRef}
          onClose={() => setFlyoutOpen(false)}
          minWidthPx={12 * 16}
          maxWidthPx={18 * 16}
        >
          <p className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Tracking
          </p>
          <ul className="max-h-[70vh] overflow-auto py-1" role="menu">
            {adminPipelineDropdownItems.map(({ href, label, icon: Icon, iconClassName }) => {
              const active = pathname === href || pathname.startsWith(`${href}/`);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    role="menuitem"
                    onClick={() => {
                      setFlyoutOpen(false);
                      onNavigate?.();
                    }}
                    className={cn(
                      "flex items-center gap-3 px-3 py-2 text-sm",
                      active
                        ? "bg-muted font-medium text-foreground"
                        : "text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                    )}
                  >
                    <Icon className={cn("h-4 w-4 shrink-0", iconClassName)} aria-hidden />
                    <span className="truncate">{label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </CollapsedSidebarFlyout>
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      <button
        type="button"
        onClick={() => setAccordionOpen((o) => !o)}
        className={cn(
          "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition-colors",
          rowActive
            ? "bg-foreground text-background"
            : "text-muted-foreground hover:bg-muted hover:text-foreground"
        )}
        aria-expanded={accordionOpen}
      >
        <ListTree className={cn("h-5 w-5 shrink-0", ADMIN_PIPELINE_ICON_CLASS)} aria-hidden />
        <span className="min-w-0 flex-1">Tracking</span>
        <ChevronRight
          className={cn("h-4 w-4 shrink-0 transition-transform duration-200", accordionOpen && "rotate-90")}
          aria-hidden
        />
      </button>
      {accordionOpen && (
        <ul className="ml-2 space-y-0.5 border-l border-border py-0.5 pl-2" role="list">
          {adminPipelineDropdownItems.map(({ href, label, icon: Icon, iconClassName }) => {
            const active = pathname === href || pathname.startsWith(`${href}/`);
            return (
              <li key={href}>
                <Link
                  href={href}
                  onClick={() => onNavigate?.()}
                  className={cn(
                    "flex items-center gap-2 rounded-md px-2 py-2 text-sm transition-colors",
                    active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted"
                  )}
                >
                  <Icon className={cn("h-4 w-4 shrink-0", iconClassName)} aria-hidden />
                  <span className="truncate">{label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function Sidebar({
  collapsed,
  onToggle,
  navScrollRef,
  navScrollContentRef,
  isMobile = false,
  mobileOpen = false,
  onMobileClose,
}: {
  collapsed: boolean;
  onToggle: () => void;
  /** Scrollport for Lenis (dashboard layout); smooth scroll matches main content. */
  navScrollRef?: React.Ref<HTMLElement>;
  navScrollContentRef?: React.Ref<HTMLDivElement>;
  isMobile?: boolean;
  mobileOpen?: boolean;
  onMobileClose?: () => void;
}) {
  const pathname = usePathname();
  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );
  const [isAdmin, setIsAdmin] = useState(false);
  const [isManagerRole, setIsManagerRole] = useState(false);
  const [managerDepartment, setManagerDepartment] = useState<string | null>(null);
  const [roleDisplay, setRoleDisplay] = useState<string>("Member");
  const adminEmailsEnv = typeof window !== "undefined" ? (process.env.NEXT_PUBLIC_ADMIN_EMAILS ?? "") : (process.env.NEXT_PUBLIC_ADMIN_EMAILS ?? "");
  const adminEmailsList = useMemo(() => adminEmailsEnv.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean), [adminEmailsEnv]);
  useEffect(() => {
    if (status !== "authenticated" || !session?.user?.email) {
      setIsAdmin(false);
      setIsManagerRole(false);
      setManagerDepartment(null);
      setRoleDisplay("Member");
      return;
    }
    const userEmail = (session.user.email ?? "").trim().toLowerCase();
    const isInEnvList = adminEmailsList.length > 0 && adminEmailsList.includes(userEmail);

    const loadMe = () => {
      api
        .getMe()
        .then((r) => {
          const effectiveAdmin = r.isAdmin || isInEnvList;
          setIsAdmin(effectiveAdmin);
          setIsManagerRole((r.role ?? "").trim() === "Manager");
          const department = (r.department ?? "").trim() || null;
          setManagerDepartment(department);
          setRoleDisplay(formatSidebarRoleLabel(r.role ?? "Member", department, effectiveAdmin));
        })
        .catch(() => {
          setIsAdmin(isInEnvList);
          setIsManagerRole(false);
          setManagerDepartment(null);
          setRoleDisplay(isInEnvList ? "Admin" : "Member");
        });
    };

    loadMe();
    const onMeSync = () => loadMe();
    window.addEventListener(ME_UPDATED_EVENT, onMeSync);
    return () => window.removeEventListener(ME_UPDATED_EVENT, onMeSync);
  }, [status, session?.user?.email, api, adminEmailsList]);
  const showManagerAdminNav = isManagerRole && !isAdmin;
  const showFullAdminNav = isAdmin;
  const showElevatedAdminNav = showFullAdminNav || showManagerAdminNav;
  const name = session?.user?.name ?? session?.user?.email ?? "User";
  const navCollapsed = collapsed && !isMobile;
  const profileImage = (session?.user?.image ?? "").trim() || null;
  const [avatarImgFailed, setAvatarImgFailed] = useState(false);
  useEffect(() => {
    setAvatarImgFailed(false);
  }, [profileImage]);

  const renderElevatedAdminLink = (item: SidebarNavItem) => {
    const { href, label, icon: Icon, iconClassName } = item;
    const isActive = pathname === href || pathname.startsWith(`${href}/`);
    return (
      <Link
        key={href}
        href={href}
        onClick={() => isMobile && onMobileClose?.()}
        className={cn(
          "flex min-w-0 items-center overflow-hidden rounded-lg py-2.5 text-sm font-medium transition-[padding,gap,color,background-color] " +
          SIDEBAR_LAYOUT_ANIM,
          isActive
            ? "bg-foreground text-background"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
          navCollapsed ? "justify-center gap-0 px-2" : "gap-3 px-3"
        )}
        title={navCollapsed ? label : undefined}
      >
        <Icon className={cn("h-5 w-5 shrink-0", iconClassName)} />
        <span
          className={cn(
            "truncate transition-opacity " + SIDEBAR_LABEL_FADE,
            navCollapsed ? "w-0 shrink-0 overflow-hidden opacity-0" : "min-w-0 flex-1 opacity-100"
          )}
          aria-hidden={navCollapsed}
        >
          {label}
        </span>
      </Link>
    );
  };

  const isHrTeamMember = (managerDepartment ?? "").trim().toLowerCase() === "hr";

  const renderExternalNavLink = ({ href, label, icon: Icon, iconClassName }: SidebarNavItem) => (
    <a
      key={href}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={() => isMobile && onMobileClose?.()}
      className={cn(
        "flex min-w-0 items-center overflow-hidden rounded-lg py-2.5 text-sm font-medium transition-[padding,gap,color,background-color] " +
          SIDEBAR_LAYOUT_ANIM,
        "text-muted-foreground hover:bg-muted hover:text-foreground",
        navCollapsed ? "justify-center gap-0 px-2" : "gap-3 px-3"
      )}
      title={navCollapsed ? label : undefined}
    >
      <Icon className={cn("h-5 w-5 shrink-0", iconClassName)} />
      <span
        className={cn(
          "truncate transition-opacity " + SIDEBAR_LABEL_FADE,
          navCollapsed ? "w-0 shrink-0 overflow-hidden opacity-0" : "min-w-0 flex-1 opacity-100"
        )}
        aria-hidden={navCollapsed}
      >
        {label}
      </span>
    </a>
  );

  const renderNavLink = ({ href, label, icon: Icon, iconClassName }: SidebarNavItem) => {
    let active = false;
    if (href === "/dashboard") active = pathname === "/dashboard";
    else if (href === "/emails")
      active = pathname === "/emails" || pathname.startsWith("/emails/");
    else active = pathname === href || pathname.startsWith(`${href}/`);
    return (
      <Link
        key={href}
        href={href}
        onClick={() => isMobile && onMobileClose?.()}
        className={cn(
          "flex min-w-0 items-center overflow-hidden rounded-lg py-2.5 text-sm font-medium transition-[padding,gap,color,background-color] " +
          SIDEBAR_LAYOUT_ANIM,
          active
            ? "bg-foreground text-background"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
          navCollapsed ? "justify-center gap-0 px-2" : "gap-3 px-3"
        )}
        title={navCollapsed ? label : undefined}
      >
        <Icon className={cn("h-5 w-5 shrink-0", iconClassName)} />
        <span
          className={cn(
            "truncate transition-opacity " + SIDEBAR_LABEL_FADE,
            navCollapsed ? "w-0 shrink-0 overflow-hidden opacity-0" : "min-w-0 flex-1 opacity-100"
          )}
          aria-hidden={navCollapsed}
        >
          {label}
        </span>
      </Link>
    );
  };

  return (
    <aside
      className={cn(
        "glass-sidebar-shell flex shrink-0 flex-col overflow-x-hidden [contain:layout] " +
        SIDEBAR_LAYOUT_ANIM,
        isMobile
          ? cn(
            "fixed inset-y-0 left-0 z-[560] w-[min(18rem,88vw)] pt-[env(safe-area-inset-top,0px)] shadow-xl transition-transform motion-reduce:transition-none",
            mobileOpen ? "translate-x-0" : "-translate-x-full pointer-events-none"
          )
          : cn("transition-[width]", navCollapsed ? "w-24" : "w-64")
      )}
      aria-hidden={isMobile && !mobileOpen}
    >
      <div
        className={cn(
          "glass-sidebar-header flex h-16 shrink-0 items-center transition-[padding,gap] " +
          SIDEBAR_LAYOUT_ANIM,
          navCollapsed
            ? "justify-center gap-2 overflow-visible px-2"
            : "gap-3 overflow-hidden px-4"
        )}
      >
        <div
          className="relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-full bg-muted ring-1 ring-border/80"
          title={name}
          aria-label={`${name} profile`}
        >
          {profileImage && !avatarImgFailed ? (
            <img
              src={profileImage}
              alt=""
              className="h-full w-full object-cover"
              onError={() => setAvatarImgFailed(true)}
            />
          ) : (
            <span className="select-none text-[11px] font-semibold leading-none text-foreground" aria-hidden>
              {userInitials(name)}
            </span>
          )}
        </div>
        <div
          className={cn(
            "min-w-0 overflow-hidden leading-snug transition-opacity " + SIDEBAR_LABEL_FADE,
            navCollapsed ? "hidden" : "max-w-[min(100%,12rem)] flex-1 opacity-100"
          )}
          aria-hidden={navCollapsed}
        >
          <p className="truncate text-base font-bold leading-snug tracking-tight text-foreground">{name}</p>
          <p className="mt-1 truncate text-sm font-semibold leading-snug text-muted-foreground">{roleDisplay}</p>
        </div>
        <button
          type="button"
          onClick={() => {
            if (isMobile) onMobileClose?.();
            else onToggle();
          }}
          className={cn(
            "flex shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors " +
            SIDEBAR_LABEL_FADE,
            "hover:bg-muted hover:text-foreground",
            navCollapsed && !isMobile ? "h-9 w-9" : "h-10 w-10"
          )}
          aria-label={isMobile ? "Close navigation menu" : navCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {navCollapsed && !isMobile ? (
            <ChevronRight className="h-5 w-5" aria-hidden />
          ) : (
            <PanelLeftClose className="h-6 w-6" aria-hidden />
          )}
        </button>
      </div>

      <nav
        ref={navScrollRef}
        className={cn(
          "glass-sidebar-nav flex min-h-0 flex-1 overflow-auto py-2 transition-[padding] " + SIDEBAR_LAYOUT_ANIM,
          navCollapsed ? "px-2" : "px-3"
        )}
      >
        <div ref={navScrollContentRef} className="w-full space-y-0.5">
          {navItemsTop.map(renderNavLink)}
          {navItemsAfterDepartments.map(renderNavLink)}
          {isHrTeamMember
            ? renderExternalNavLink({
                href: SKILLVAULT_URL,
                label: "SkillVault",
                icon: GraduationCap,
                iconClassName: "text-purple-500",
              })
            : null}
          {showElevatedAdminNav && (
            <>
              <p
                className={cn(
                  "block w-full overflow-hidden text-left text-sm font-bold uppercase tracking-wider text-muted-foreground transition-opacity " +
                  "rounded-md border border-border/50 bg-muted/40 px-2 py-2 " +
                  SIDEBAR_LABEL_FADE,
                  navCollapsed ? "pointer-events-none mt-0 mb-0 h-0 border-0 bg-transparent py-0 opacity-0" : "mt-12 mb-2 h-auto opacity-100"
                )}
                aria-hidden={navCollapsed}
              >
                {showManagerAdminNav ? "Management panel" : "Admin panel"}
              </p>
              <AdminTeamsNavSection
                collapsed={navCollapsed}
                allowedDepartment={showManagerAdminNav ? managerDepartment : null}
                onNavigate={isMobile ? onMobileClose : undefined}
              />
              {showFullAdminNav ? (
                <>
                  {adminNavBeforePipeline.map(renderElevatedAdminLink)}
                  <AdminPipelineNavSection
                    collapsed={navCollapsed}
                    onNavigate={isMobile ? onMobileClose : undefined}
                  />
                  {adminNavAfterPipeline.map(renderElevatedAdminLink)}
                </>
              ) : (
                <>
                  {managerAdminNavItems
                    .filter((item) => !ADMIN_ONLY_HREFS.has(item.href))
                    .map(renderElevatedAdminLink)}
                  <AdminPipelineNavSection
                    collapsed={navCollapsed}
                    onNavigate={isMobile ? onMobileClose : undefined}
                  />
                </>
              )}
            </>
          )}
        </div>
      </nav>
    </aside>
  );
}
