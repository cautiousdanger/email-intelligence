"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { ShieldCheck, Users, UserCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { getApi } from "@/lib/api/client";
import { ME_UPDATED_EVENT } from "@/lib/me-sync-events";
import { cn } from "@/lib/utils";

type RoleTab = "user" | "manager" | "admin";

const TABS: Array<{ id: RoleTab; label: string; icon: typeof UserCircle }> = [
  { id: "user", label: "User guide", icon: UserCircle },
  { id: "manager", label: "Manager guide", icon: Users },
  { id: "admin", label: "Admin guide", icon: ShieldCheck },
];

type TourStep = {
  id: string;
  title: string;
  description: string;
  href: string;
  role: RoleTab | "all";
};

const TOUR_STEPS: TourStep[] = [
  {
    id: "dashboard",
    title: "Dashboard",
    description: "Start here every day. You can see summary cards, meetings, projects, and your role updates.",
    href: "/dashboard?tour=1",
    role: "all",
  },
  {
    id: "history",
    title: "Mailbox",
    description: "Open all synced emails with filters. Use this when you need detailed mail-level review.",
    href: "/emails",
    role: "all",
  },
  {
    id: "threads",
    title: "Threads",
    description: "Track complete conversations and reply flow by thread to avoid missing context.",
    href: "/threads",
    role: "all",
  },
  {
    id: "departments",
    title: "Departments toggle",
    description: "Expand Departments in the sidebar to inspect emails by team/category like Sales, Tech, Accounts, and more.",
    href: "/departments/all",
    role: "all",
  },
  {
    id: "escalations",
    title: "Escalations",
    description: "Check urgent messages that need action first.",
    href: "/escalations",
    role: "all",
  },
  {
    id: "leads",
    title: "Leads",
    description: "Review potential business leads and their priority labels.",
    href: "/leads",
    role: "all",
  },
  {
    id: "retag",
    title: "ReTag",
    description: "Move wrongly tagged escalation/lead mails into the correct department.",
    href: "/retag",
    role: "all",
  },
  {
    id: "mom",
    title: "MOM",
    description: "Manage meeting follow-up and pending MOM actions.",
    href: "/mom",
    role: "all",
  },
  {
    id: "followup",
    title: "Follow UP",
    description: "Track project follow-up email compliance by day and project.",
    href: "/follow-up",
    role: "all",
  },
  {
    id: "team-leaders",
    title: "Admin: User management",
    description: "Manage users, role assignment, and login history.",
    href: "/admin/team-leaders",
    role: "admin",
  },
  {
    id: "projects",
    title: "Admin: Projects",
    description: "Create and manage team projects and assignees.",
    href: "/admin/team-projects",
    role: "admin",
  },
  {
    id: "tracker",
    title: "Admin: Tracker",
    description: "Set expected tracker days and monitor project-level tracker behavior.",
    href: "/admin/tracker",
    role: "admin",
  },
  {
    id: "review",
    title: "Admin: Review",
    description: "User-wise escalations, leads, and project tracker metrics in one table.",
    href: "/admin/review",
    role: "admin",
  },
];

/** Manual tabs: admin → admin only; manager → manager only; member → user only. */
function visibleTabsForRoles(isAdmin: boolean, isManagerRole: boolean): RoleTab[] {
  if (isAdmin) return ["admin"];
  if (isManagerRole) return ["manager"];
  return ["user"];
}

export default function HowToUsePage() {
  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );
  const adminEmailsEnv = process.env.NEXT_PUBLIC_ADMIN_EMAILS ?? "";
  const adminEmailsList = useMemo(
    () => adminEmailsEnv.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean),
    [adminEmailsEnv]
  );

  const [isAdmin, setIsAdmin] = useState(false);
  const [isManagerRole, setIsManagerRole] = useState(false);

  const [active, setActive] = useState<RoleTab>("user");
  const [tourOpen, setTourOpen] = useState(false);
  const [tourIndex, setTourIndex] = useState(0);

  useEffect(() => {
    if (status !== "authenticated" || !session?.user?.email) {
      setIsAdmin(false);
      setIsManagerRole(false);
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
        })
        .catch(() => {
          setIsAdmin(isInEnvList);
          setIsManagerRole(false);
        });
    };

    loadMe();
    const onMeSync = () => loadMe();
    window.addEventListener(ME_UPDATED_EVENT, onMeSync);
    return () => window.removeEventListener(ME_UPDATED_EVENT, onMeSync);
  }, [status, session?.user?.email, api, adminEmailsList]);

  const visibleTabs = useMemo(() => visibleTabsForRoles(isAdmin, isManagerRole), [isAdmin, isManagerRole]);

  useEffect(() => {
    const tabs = visibleTabsForRoles(isAdmin, isManagerRole);
    setActive((a) => (tabs.includes(a) ? a : tabs[0] ?? "user"));
  }, [isAdmin, isManagerRole]);

  const visibleSteps = useMemo(
    () => TOUR_STEPS.filter((s) => s.role === "all" || s.role === active),
    [active]
  );

  useEffect(() => {
    // Auto-open once for first-time users so guide explains each feature by default.
    if (typeof window === "undefined") return;
    const key = "how_to_use_tour_seen_v1";
    const seen = window.localStorage.getItem(key);
    if (!seen) {
      setTourOpen(true);
      setTourIndex(0);
      window.localStorage.setItem(key, "1");
    }
  }, []);

  useEffect(() => {
    // When switching role tab, restart from first relevant step.
    setTourIndex(0);
  }, [active]);

  const currentStep = visibleSteps[tourIndex] ?? null;
  const isFirst = tourIndex === 0;
  const isLast = tourIndex >= visibleSteps.length - 1;

  const startTour = () => {
    setTourOpen(true);
    setTourIndex(0);
  };

  return (
    <div className="min-w-0 max-w-full space-y-4 sm:space-y-6">
      <div className="min-w-0">
        <h1 className="text-xl font-bold tracking-tight text-neutral-900 dark:text-neutral-50 sm:text-2xl">
          How to use
        </h1>
        <div className="mt-3 flex flex-col gap-2 sm:mt-4 sm:flex-row sm:flex-wrap">
          <Button type="button" className="h-10 w-full sm:h-9 sm:w-auto" onClick={startTour}>
            Replay guided tour
          </Button>
          {tourOpen && (
            <Button
              type="button"
              variant="outline"
              className="h-10 w-full sm:h-9 sm:w-auto"
              onClick={() => setTourOpen(false)}
            >
              Hide tour panel
            </Button>
          )}
        </div>
      </div>

      <Card className="min-w-0 rounded-2xl border-border">
        <CardHeader className="p-4 sm:p-6">
          <CardTitle className="text-base">Role manuals</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 p-4 pt-0 sm:p-6 sm:pt-0">
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            {TABS.filter((t) => visibleTabs.includes(t.id)).map((t) => {
              const Icon = t.icon;
              return (
                <Button
                  key={t.id}
                  type="button"
                  variant={active === t.id ? "default" : "outline"}
                  onClick={() => setActive(t.id)}
                  className={cn(
                    "h-10 w-full justify-center sm:h-9 sm:w-auto sm:justify-start",
                    active === t.id && "bg-neutral-900 hover:bg-neutral-800 dark:bg-neutral-100 dark:text-neutral-900"
                  )}
                >
                  <Icon className="mr-2 h-4 w-4 shrink-0" />
                  {t.label}
                </Button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {tourOpen && currentStep && (
        <Card className="min-w-0 rounded-2xl border-indigo-200 bg-indigo-50/60 dark:border-indigo-900/60 dark:bg-indigo-950/20">
          <CardHeader className="p-4 sm:p-6">
            <CardTitle className="text-base leading-snug">
              Guided walkthrough{" "}
              <span className="tabular-nums">
                ({tourIndex + 1}/{visibleSteps.length})
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 p-4 pt-0 text-sm sm:p-6 sm:pt-0">
            <p className="font-semibold text-neutral-900 dark:text-neutral-100">{currentStep.title}</p>
            <p className="break-words leading-relaxed text-neutral-700 dark:text-neutral-300">
              {currentStep.description}
            </p>
            <p className="text-xs leading-relaxed text-neutral-600 dark:text-neutral-400">
              Open this toggle/page to see it in action.
            </p>
            <div className="grid grid-cols-1 gap-2 sm:flex sm:flex-wrap sm:items-stretch">
              <Link href={currentStep.href} className="min-w-0 sm:inline-flex">
                <Button
                  type="button"
                  size="sm"
                  className="h-auto min-h-9 w-full whitespace-normal px-3 py-2 text-center leading-snug sm:w-auto sm:text-left"
                >
                  Open {currentStep.title}
                </Button>
              </Link>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-9 w-full sm:w-auto"
                disabled={isFirst}
                onClick={() => setTourIndex((i) => Math.max(0, i - 1))}
              >
                Previous
              </Button>
              <Button
                type="button"
                size="sm"
                className="h-9 w-full sm:w-auto"
                variant={isLast ? "default" : "outline"}
                onClick={() => {
                  if (isLast) setTourIndex(0);
                  else setTourIndex((i) => Math.min(visibleSteps.length - 1, i + 1));
                }}
              >
                {isLast ? "Restart tour" : "Next feature"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {active === "manager" && (
        <Card className="min-w-0 rounded-2xl border-border">
          <CardHeader className="p-4 sm:p-6">
            <CardTitle className="text-base sm:text-lg">Manager manual</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 p-4 pt-0 text-sm leading-relaxed text-neutral-700 dark:text-neutral-300 sm:p-6 sm:pt-0">
            <p className="font-medium text-neutral-900 dark:text-neutral-100">1) Team oversight</p>
            <p className="break-words">Review team inbox trends via Dashboard and department views.</p>
            <p className="font-medium text-neutral-900 dark:text-neutral-100">2) Tracker compliance</p>
            <p className="break-words">
              Ask team members to use Follow UP and keep subjects in the format with tracker + project name so sends are
              detected.
            </p>
            <p className="font-medium text-neutral-900 dark:text-neutral-100">3) Escalation response</p>
            <p className="break-words">Track pending escalations and ensure users are replying in time.</p>
          </CardContent>
        </Card>
      )}

      {active === "admin" && (
        <Card className="min-w-0 rounded-2xl border-border">
          <CardHeader className="p-4 sm:p-6">
            <CardTitle className="text-base sm:text-lg">Admin manual</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 p-4 pt-0 text-sm leading-relaxed text-neutral-700 dark:text-neutral-300 sm:p-6 sm:pt-0">
            <p className="font-medium text-neutral-900 dark:text-neutral-100">1) Setup</p>
            <p className="break-words">Create teams, users, reporting workflow, and projects in Admin pages.</p>
            <p className="font-medium text-neutral-900 dark:text-neutral-100">2) Tracker configuration</p>
            <p className="break-words">
              In Admin → Tracker, set expected weekdays per project and monitor project history.
            </p>
            <p className="font-medium text-neutral-900 dark:text-neutral-100">3) Review module</p>
            <p className="break-words">
              Use Admin → Review for a single user-wise view: escalation and lead reply counts plus tracker deadlines
              and sends in the selected date range.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
