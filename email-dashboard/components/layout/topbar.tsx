"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { signOut } from "next-auth/react";
import {
  Calendar,
  LogOut,
  Bell,
  ArrowLeft,
  RefreshCw,
  Mail,
  Inbox,
  AlertCircle,
  Sparkles,
  MessageSquare,
  Target,
  ClipboardList,
  ChevronRight,
  Menu,
  Layers,
  Tags,
  BellRing,
  X,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ShortcutFeatureOverlay, type ShortcutFeatureId } from "@/components/shortcuts/shortcut-feature-overlay";
import { getApi } from "@/lib/api/client";
import { ME_UPDATED_EVENT } from "@/lib/me-sync-events";
import { LenisScrollArea } from "@/components/lenis/lenis-scroll-area";
import { cn } from "@/lib/utils";
import type { NotificationItem, SystemStatus } from "@/lib/types";
import {
  CLASSIFY_BATCH_SUMMARY_EVENT,
  type ClassifyBatchSummaryDetail,
} from "@/lib/classify-batch-summary-event";

const NOTIF_GROUP_ORDER = ["priority", "mail", "sales", "ai", "meetings", "other"] as const;
const NOTIF_GROUP_LABEL: Record<string, string> = {
  priority: "Needs attention",
  mail: "Mail",
  sales: "Leads",
  ai: "AI",
  meetings: "Meetings & calendar",
  other: "Updates",
};

function notificationIcon(kind: string): LucideIcon {
  switch (kind) {
    case "escalation_open":
      return AlertCircle;
    case "lead_open":
      return Target;
    case "unreplied_mail":
      return MessageSquare;
    case "unread_mail":
      return Mail;
    case "new_mail":
      return Inbox;
    case "ai_pending":
      return Sparkles;
    case "mom_pending":
      return ClipboardList;
    case "meeting_upcoming":
    case "meeting_scheduled":
    case "important_date":
      return Calendar;
    default:
      return Bell;
  }
}

/**
 * Labels aligned with `sidebar.tsx` nav items (same href → same visible name).
 */
const pathToLabel: Record<string, string> = {
  "/dashboard": "Dashboard",
  "/emails": "Mailbox",
  "/daily-digest": "Daily digest",
  "/threads": "Threads",
  "/departments": "Departments",
  "/escalations": "Escalations",
  "/leads": "Leads",
  "/follow-up": "Follow UP",
  "/mom": "MOM",
  "/retag": "ReTag",
  "/how-to-use": "How to use",
  "/profile": "Profile",
  "/webhook": "Webhook",
  "/queue": "Queue",
  "/settings": "Settings",
  "/admin/my-projects": "Projects",
  "/admin/team-leaders": "User management",
  "/admin/team-projects": "Projects",
  "/admin/tracker": "Tracker",
  "/admin/review": "Review",
  "/admin/workflow": "Hierarchy",
  "/admin/escalations": "Escalations",
  "/admin/leads": "Leads",
  "/admin/approvals": "Approvals",
  "/admin/archive-projects": "Archive Projects",
  "/admin/teams": "Teams",
  "/admin/projects": "Projects",
};

function prettifySegment(seg: string): string {
  if (!seg) return "";
  const v = decodeURIComponent(seg).replace(/[-_]/g, " ").trim();
  if (!v) return "";
  return v.charAt(0).toUpperCase() + v.slice(1);
}

function toEmailDisplayNumber(rawId: string): string {
  const normalized = (rawId || "").toLowerCase().replace(/[^0-9a-f]/g, "");
  if (!normalized) return "Email";
  const seed = normalized.slice(0, 12);
  const n = Number.parseInt(seed, 16);
  if (!Number.isFinite(n) || Number.isNaN(n)) return "Email";
  const unique = (n % 900000) + 100000;
  return `Email #${unique}`;
}

function isLikelyUuidSegment(seg: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(seg.trim());
}

type BreadcrumbItem = { label: string; href: string };

function buildBreadcrumbItems(
  pathname: string,
  trackerProjectLabel: string | null,
  adminRootLabel: string
): BreadcrumbItem[] {
  if (!pathname || pathname === "/") {
    return [{ label: "Dashboard", href: "/dashboard" }];
  }

  const norm = pathname.replace(/\/$/, "") || "/";

  const deptMatch = norm.match(/^\/departments\/([^/]+)$/);
  if (deptMatch) {
    const seg = deptMatch[1];
    const secondLabel = seg.toLowerCase() === "all" ? "All" : prettifySegment(seg);
    return [
      { label: "Departments", href: "/departments/all" },
      { label: secondLabel, href: norm },
    ];
  }

  const segments = norm.split("/").filter(Boolean);
  if (segments.length === 0) {
    return [{ label: "Dashboard", href: "/dashboard" }];
  }

  const items: BreadcrumbItem[] = [];
  let acc = "";
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    acc += `/${seg}`;
    let href = acc;
    if (href === "/departments") {
      href = "/departments/all";
    }
    if (acc === "/admin") {
      href = "/dashboard";
      items.push({ label: adminRootLabel, href });
      continue;
    }
    let label = pathToLabel[acc];
    if (!label) {
      if (acc === norm && norm.startsWith("/admin/tracker/") && trackerProjectLabel) {
        label = trackerProjectLabel;
      } else if (acc === norm && norm.startsWith("/emails/")) {
        label = toEmailDisplayNumber(seg);
      } else if (acc === norm && norm.startsWith("/admin/teams/") && isLikelyUuidSegment(seg)) {
        label = "Team";
      } else if (acc === norm && norm.startsWith("/admin/team-projects/") && isLikelyUuidSegment(seg)) {
        label = "Project";
      } else {
        label = prettifySegment(seg);
      }
    }
    items.push({ label, href });
  }
  return items;
}

export function Topbar({
  environment = "Dev",
  onOpenMobileNav,
}: {
  systemStatus?: SystemStatus;
  environment?: string;
  onOpenMobileNav?: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );
  const [trackerProjectLabel, setTrackerProjectLabel] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [shortcutFeature, setShortcutFeature] = useState<ShortcutFeatureId | null>(null);
  const notifTriggerRef = useRef<HTMLDivElement>(null);
  const toolsTriggerRef = useRef<HTMLDivElement>(null);
  const notifPanelRef = useRef<HTMLDivElement>(null);
  const toolsPanelRef = useRef<HTMLDivElement>(null);
  const [notifPos, setNotifPos] = useState<{ top: number; left: number } | null>(null);
  const [toolsPos, setToolsPos] = useState<{ top: number; left: number } | null>(null);
  const [portalMounted, setPortalMounted] = useState(false);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [notifError, setNotifError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [classifyBatchSummary, setClassifyBatchSummary] = useState<ClassifyBatchSummaryDetail | null>(null);
  const [isAdminEffective, setIsAdminEffective] = useState(false);
  const [isManagerRole, setIsManagerRole] = useState(false);
  const [meHydrated, setMeHydrated] = useState(false);
  const adminEmailsEnv = process.env.NEXT_PUBLIC_ADMIN_EMAILS ?? "";
  const adminEmailsList = useMemo(
    () => adminEmailsEnv.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean),
    [adminEmailsEnv]
  );

  useEffect(() => {
    if (status === "loading") return;
    if (status !== "authenticated" || !session?.user?.email) {
      setIsAdminEffective(false);
      setIsManagerRole(false);
      setMeHydrated(true);
      return;
    }
    const userEmail = (session.user.email ?? "").trim().toLowerCase();
    const isInEnvList = adminEmailsList.length > 0 && adminEmailsList.includes(userEmail);

    const loadMe = (fromMeSync: boolean) => {
      if (!fromMeSync) setMeHydrated(false);
      api
        .getMe()
        .then((r) => {
          const role = (r.role ?? "").trim();
          setIsAdminEffective(r.isAdmin || isInEnvList);
          setIsManagerRole(role === "Manager");
        })
        .catch(() => {
          setIsAdminEffective(isInEnvList);
          setIsManagerRole(false);
        })
        .finally(() => setMeHydrated(true));
    };

    loadMe(false);
    const onMeSync = () => loadMe(true);
    window.addEventListener(ME_UPDATED_EVENT, onMeSync);
    return () => window.removeEventListener(ME_UPDATED_EVENT, onMeSync);
  }, [status, session?.user?.email, api, adminEmailsList]);

  const adminRootLabel = useMemo(() => {
    if (!meHydrated) return "Admin";
    if (isAdminEffective) return "Admin";
    if (isManagerRole) return "Management";
    return "Workspace";
  }, [meHydrated, isAdminEffective, isManagerRole]);

  const breadcrumbItems = useMemo(
    () => buildBreadcrumbItems(pathname, trackerProjectLabel, adminRootLabel),
    [pathname, trackerProjectLabel, adminRootLabel]
  );

  const loadNotifications = () => {
    if (status !== "authenticated") return;
    setLoading(true);
    api
      .getNotifications()
      .then((r) => {
        setItems(r.items ?? []);
        setNotifError(r.error ?? null);
      })
      .catch(() => {
        setNotifError("Could not load updates.");
        setItems([]);
      })
      .finally(() => setLoading(false));
  };

  const groupedNotifications = useMemo(() => {
    const allowed = new Set<string>(NOTIF_GROUP_ORDER);
    const map = new Map<string, NotificationItem[]>();
    for (const n of items) {
      const raw = (n.group || "other").trim() || "other";
      const key = allowed.has(raw) ? raw : "other";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(n);
    }
    return NOTIF_GROUP_ORDER.filter((k) => map.has(k)).map((k) => ({
      key: k,
      label: NOTIF_GROUP_LABEL[k] ?? k,
      items: map.get(k)!,
    }));
  }, [items]);

  const bellBadgeCount = useMemo(() => {
    if (items.length === 0) return 0;
    const warnings = items.filter((i) => i.level === "warning");
    const fromWarnings = warnings.reduce((s, i) => s + (i.count ?? 1), 0);
    if (fromWarnings > 0) return Math.min(99, fromWarnings);
    return Math.min(9, items.length);
  }, [items]);

  useEffect(() => {
    if (status !== "authenticated") return;
    loadNotifications();
    const id = window.setInterval(loadNotifications, 60000);
    return () => window.clearInterval(id);
  }, [status, api]);

  useEffect(() => {
    const onBatchSummary = (e: Event) => {
      const ce = e as CustomEvent<ClassifyBatchSummaryDetail>;
      const d = ce.detail;
      if (d?.summary) {
        setClassifyBatchSummary({ summary: d.summary, count: d.count });
        setToolsOpen(false);
        setShortcutFeature(null);
        setOpen(true);
      }
    };
    window.addEventListener(CLASSIFY_BATCH_SUMMARY_EVENT, onBatchSummary);
    return () => window.removeEventListener(CLASSIFY_BATCH_SUMMARY_EVENT, onBatchSummary);
  }, []);

  useEffect(() => {
    if (status !== "authenticated") return;
    const m = pathname.match(/^\/admin\/tracker\/([^/]+)$/);
    if (!m) {
      setTrackerProjectLabel(null);
      return;
    }
    const projectId = decodeURIComponent(m[1]);
    api
      .getAdminTrackerProjectEmails(projectId, { days: 30, limit: 1 })
      .then((r) => {
        const name = (r.projectName || "").trim();
        setTrackerProjectLabel(name || "Project");
      })
      .catch(() => setTrackerProjectLabel("Project"));
  }, [pathname, status, api]);

  useEffect(() => setPortalMounted(true), []);

  useEffect(() => {
    setShortcutFeature(null);
  }, [pathname]);

  const panelBelowTrigger = (el: HTMLElement | null, panelW: number) => {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    let left = rect.right - panelW;
    const vw = window.innerWidth;
    if (left < 8) left = 8;
    if (left + panelW > vw - 8) left = Math.max(8, vw - panelW - 8);
    return { top: rect.bottom + 8, left };
  };

  useLayoutEffect(() => {
    if (!open) setNotifPos(null);
    if (!toolsOpen) setToolsPos(null);
    const update = () => {
      if (open) {
        const p = panelBelowTrigger(notifTriggerRef.current, 400);
        if (p) setNotifPos(p);
      }
      if (toolsOpen) {
        const p = panelBelowTrigger(toolsTriggerRef.current, 320);
        if (p) setToolsPos(p);
      }
    };
    if (!open && !toolsOpen) return;
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, toolsOpen]);

  useEffect(() => {
    if (!open && !toolsOpen) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (notifTriggerRef.current?.contains(t)) return;
      if (notifPanelRef.current?.contains(t)) return;
      if (toolsTriggerRef.current?.contains(t)) return;
      if (toolsPanelRef.current?.contains(t)) return;
      setOpen(false);
      setToolsOpen(false);
    };
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, [open, toolsOpen]);

  useEffect(() => {
    if (!open && !toolsOpen && !shortcutFeature) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        setToolsOpen(false);
        setShortcutFeature(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, toolsOpen, shortcutFeature]);

  const currentPageLabel =
    breadcrumbItems.length > 0 ? breadcrumbItems[breadcrumbItems.length - 1].label : "Dashboard";

  return (
    <header className="glass-app-chrome flex min-h-16 items-center justify-between gap-2 px-2 pt-[env(safe-area-inset-top,0px)] sm:px-4 md:gap-4 md:px-5">
      {/* Back + Breadcrumbs */}
      <div className="flex min-w-0 flex-1 items-center gap-1.5 sm:gap-2.5">
        {onOpenMobileNav ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-10 w-10 shrink-0 rounded-lg md:hidden"
            aria-label="Open navigation menu"
            onClick={onOpenMobileNav}
          >
            <Menu className="h-5 w-5" />
          </Button>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="hidden h-10 w-10 shrink-0 rounded-lg md:inline-flex"
          aria-label="Go back"
          onClick={() => router.back()}
        >
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <Calendar className="hidden h-5 w-5 shrink-0 text-muted-foreground md:block" aria-hidden />
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-0.5 leading-tight md:hidden">
          <Link
            href="/dashboard"
            className="truncate text-[10px] font-semibold uppercase tracking-wide text-muted-foreground underline-offset-2 hover:underline"
          >
            Email Intelligence
          </Link>
          <p className="truncate text-sm font-semibold text-foreground" title={currentPageLabel}>
            {currentPageLabel}
          </p>
        </div>
        <nav
          className="hidden min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm md:flex md:gap-x-2 md:text-base"
          aria-label="Breadcrumb"
        >
          <Link
            href="/dashboard"
            className="shrink-0 truncate font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          >
            Email Intelligence
          </Link>
          {breadcrumbItems.map((item, idx) => (
            <span key={`${item.href}-${idx}`} className="flex min-w-0 items-center gap-1.5">
              <span className="shrink-0 text-muted-foreground/70" aria-hidden>
                &gt;
              </span>
              <Link
                href={item.href}
                className={cn(
                  "min-w-0 truncate underline-offset-2 hover:underline",
                  idx === breadcrumbItems.length - 1
                    ? "font-semibold text-foreground hover:text-foreground"
                    : "font-medium text-muted-foreground hover:text-foreground"
                )}
              >
                {item.label}
              </Link>
            </span>
          ))}
        </nav>
      </div>

      {/* Right actions */}
      <div className="flex shrink-0 items-center gap-0.5">
        <div className="relative" ref={toolsTriggerRef}>
          <Button
            variant="ghost"
            size="icon"
            className="h-10 w-10 rounded-lg"
            aria-label="Action: MOM, ReTag, Follow UP"
            aria-expanded={toolsOpen}
            onClick={() => {
              setOpen(false);
              setShortcutFeature(null);
              setToolsOpen((v) => !v);
            }}
          >
            <Layers className="h-5 w-5" />
          </Button>
        </div>
        <div className="relative" ref={notifTriggerRef}>
          <Button
            variant="ghost"
            size="icon"
            className="h-10 w-10 rounded-lg"
            aria-label="Notifications"
            aria-expanded={open}
            onClick={() => {
              setToolsOpen(false);
              setShortcutFeature(null);
              setOpen((v) => {
                const next = !v;
                if (!v) loadNotifications();
                return next;
              });
            }}
          >
            <Bell className="h-5 w-5" />
            {bellBadgeCount > 0 && (
              <span className="absolute right-1 top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-red-600 px-1 text-[10px] font-semibold text-white tabular-nums">
                {bellBadgeCount > 99 ? "99+" : bellBadgeCount}
              </span>
            )}
          </Button>
        </div>
        {portalMounted &&
          toolsOpen &&
          toolsPos &&
          createPortal(
            <div
              ref={toolsPanelRef}
              className="fixed w-[min(100vw-1rem,320px)] overflow-hidden rounded-xl border border-border/90 bg-panel/95 shadow-xl backdrop-blur-md dark:border-border/70 dark:bg-panel/95"
              style={{ top: toolsPos.top, left: toolsPos.left, zIndex: 500 }}
              role="dialog"
              aria-label="Action"
            >
              <div className="border-b border-border/80 px-4 py-3">
                <p className="text-sm font-semibold text-foreground">Action</p>
              </div>
              <div className="space-y-0.5 p-2">
                <button
                  type="button"
                  className="flex w-full gap-3 rounded-lg px-3 py-3 text-left outline-none transition-colors hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => {
                    setToolsOpen(false);
                    setShortcutFeature("mom");
                  }}
                >
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted/90 text-muted-foreground">
                    <ClipboardList className="h-5 w-5 shrink-0" aria-hidden />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">MOM</p>
                    <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                      Meeting minutes status and history
                    </p>
                  </div>
                  <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground/45" aria-hidden />
                </button>
                <button
                  type="button"
                  className="flex w-full gap-3 rounded-lg px-3 py-3 text-left outline-none transition-colors hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => {
                    setToolsOpen(false);
                    setShortcutFeature("retag");
                  }}
                >
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted/90 text-muted-foreground">
                    <Tags className="h-5 w-5 shrink-0" aria-hidden />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">ReTag</p>
                    <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                      Mail moved to another department from escalations or leads
                    </p>
                  </div>
                  <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground/45" aria-hidden />
                </button>
                <button
                  type="button"
                  className="flex w-full gap-3 rounded-lg px-3 py-3 text-left outline-none transition-colors hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => {
                    setToolsOpen(false);
                    setShortcutFeature("follow-up");
                  }}
                >
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted/90 text-muted-foreground">
                    <BellRing className="h-5 w-5 shrink-0" aria-hidden />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">Follow UP</p>
                    <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                      Tracker schedules, reminders, and send history
                    </p>
                  </div>
                  <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground/45" aria-hidden />
                </button>
              </div>
            </div>,
            document.body
          )}
        {portalMounted &&
          open &&
          notifPos &&
          createPortal(
            <div
              ref={notifPanelRef}
              className="fixed w-[min(100vw-1rem,400px)] overflow-hidden rounded-xl border border-border/90 bg-panel/95 shadow-xl backdrop-blur-md dark:border-border/70 dark:bg-panel/95"
              style={{ top: notifPos.top, left: notifPos.left, zIndex: 500 }}
            >
              <div className="flex items-center justify-between gap-3 border-b border-border/80 px-4 py-3">
                <p className="text-sm font-semibold text-foreground">Your updates</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 shrink-0 rounded-lg text-muted-foreground"
                  aria-label="Refresh updates"
                  onClick={() => loadNotifications()}
                  disabled={loading}
                >
                  <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
                </Button>
              </div>
              {notifError && (
                <p className="border-b border-amber-500/25 bg-amber-500/10 px-4 py-2 text-xs text-amber-900 dark:text-amber-200">
                  {notifError}
                </p>
              )}
              <LenisScrollArea className="max-h-[min(70vh,22rem)] min-h-0">
                {classifyBatchSummary ? (
                  <div className="border-b border-border/70 bg-muted/15 p-3 dark:bg-muted/10">
                    <div className="flex items-start justify-between gap-2">
                      <p className="px-0.5 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                        Classification recap
                      </p>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 shrink-0 rounded-lg text-muted-foreground hover:text-foreground"
                        aria-label="Dismiss classification recap"
                        onClick={() => setClassifyBatchSummary(null)}
                      >
                        <X className="h-4 w-4" aria-hidden />
                      </Button>
                    </div>
                    <p className="mt-1 whitespace-pre-wrap px-0.5 text-xs leading-relaxed text-foreground">
                      {classifyBatchSummary.summary}
                    </p>
                    <p className="mt-1.5 px-0.5 text-[10px] text-muted-foreground">
                      {classifyBatchSummary.count} messages summarized together
                    </p>
                  </div>
                ) : null}
                {loading && items.length === 0 && !classifyBatchSummary ? (
                  <p className="px-4 py-8 text-center text-sm text-muted-foreground">Loading updates…</p>
                ) : items.length === 0 && !classifyBatchSummary ? (
                  notifError ? (
                    <p className="px-4 py-8 text-center text-sm text-muted-foreground">
                      Couldn&apos;t load this list. Fix the message above if shown, then tap refresh.
                    </p>
                  ) : (
                    <div className="px-4 py-10 text-center">
                      <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted/80 text-muted-foreground">
                        <Bell className="h-6 w-6 opacity-70" />
                      </div>
                      <p className="text-sm font-medium text-foreground">You&apos;re all caught up</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        No escalations, leads, or alerts right now.
                      </p>
                    </div>
                  )
                ) : items.length === 0 && classifyBatchSummary ? (
                  <p className="px-4 py-4 text-center text-xs text-muted-foreground">No other alerts right now.</p>
                ) : (
                  <div className="space-y-1 p-2">
                    {groupedNotifications.map(({ key, label, items: groupItems }, gi) => (
                      <div key={key}>
                        {groupedNotifications.length > 1 && (
                          <p
                            className={cn(
                              "px-2 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground",
                              gi === 0 && "pt-0"
                            )}
                          >
                            {label}
                          </p>
                        )}
                        <ul className="space-y-0.5">
                          {groupItems.map((n) => {
                            const Icon = notificationIcon(n.kind);
                            const body = (
                              <div
                                className={cn(
                                  "flex gap-3 rounded-lg px-2 py-2.5 transition-colors",
                                  n.level === "warning"
                                    ? "bg-amber-500/[0.08] dark:bg-amber-500/10"
                                    : "hover:bg-muted/70"
                                )}
                              >
                                <div
                                  className={cn(
                                    "flex h-9 w-9 shrink-0 items-center justify-center rounded-full",
                                    n.level === "warning"
                                      ? "bg-amber-500/15 text-amber-800 dark:text-amber-300"
                                      : "bg-muted/90 text-muted-foreground"
                                  )}
                                >
                                  <Icon className="h-4 w-4 shrink-0" aria-hidden />
                                </div>
                                <div className="min-w-0 flex-1">
                                  <p className="text-sm font-medium leading-snug text-foreground">{n.title}</p>
                                  <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{n.message}</p>
                                </div>
                                {n.href ? (
                                  <ChevronRight
                                    className="mt-1 h-4 w-4 shrink-0 text-muted-foreground/45"
                                    aria-hidden
                                  />
                                ) : null}
                              </div>
                            );
                            return (
                              <li key={n.id}>
                                {n.href ? (
                                  <Link href={n.href} className="block rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => setOpen(false)}>
                                    {body}
                                  </Link>
                                ) : (
                                  body
                                )}
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    ))}
                  </div>
                )}
              </LenisScrollArea>
            </div>,
            document.body
          )}
        <ShortcutFeatureOverlay
          portalMounted={portalMounted}
          feature={shortcutFeature}
          onClose={() => setShortcutFeature(null)}
        />
        <Button
          variant="ghost"
          size="icon"
          onClick={async () => {
            try {
              await api.recordLogout();
            } catch {
              // Best-effort logout audit; sign out regardless.
            }
            void signOut({ callbackUrl: "/signin" });
          }}
          className="h-10 w-10 rounded-lg"
          aria-label="Sign out"
        >
          <LogOut className="h-5 w-5" />
        </Button>
      </div>
    </header>
  );
}
