"use client";

import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { useSession } from "next-auth/react";
import { getApi } from "@/lib/api/client";
import type { UserOut, TeamOut } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { UserCircle, Users } from "lucide-react";

function activityTimestampMs(iso: string | null | undefined): number {
  if (!iso?.trim()) return 0;
  const t = new Date(iso).getTime();
  return isNaN(t) ? 0 : t;
}

function formatActivity(iso: string | null | undefined): string {
  if (!iso?.trim()) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function userDisplayLabel(u: UserOut): string {
  return u.displayName?.trim() || u.email.split("@")[0] || u.email;
}

function sortUsersByName(a: UserOut, b: UserOut): number {
  return (a.displayName ?? a.email).localeCompare(b.displayName ?? b.email, undefined, {
    sensitivity: "base",
  });
}

function formatEmailCount(n: number | undefined | null): string {
  const v = typeof n === "number" && Number.isFinite(n) ? n : 0;
  return v.toLocaleString();
}

/** Poll interval while User management is open (matches dashboard mail sync refresh). */
const EMAIL_COUNT_POLL_MS = 2500;

export default function AdminTeamLeadersPage() {
  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );
  const [managers, setManagers] = useState<UserOut[]>([]);
  const [teams, setTeams] = useState<TeamOut[]>([]);
  const [allUsers, setAllUsers] = useState<UserOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const emailCountPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [me, setMe] = useState<{
    userId?: string;
    isAdmin?: boolean;
    role?: string;
    department?: string | null;
  } | null>(null);
  const canEdit = !!me?.isAdmin;
  const isManagerRole = (me?.role ?? "").trim() === "Manager";
  const canAssignReportingTo = canEdit || isManagerRole;

  const sortedAllUsers = useMemo(() => {
    const score = (u: UserOut) => activityTimestampMs(u.lastLoginAt) || activityTimestampMs(u.createdAt);
    return [...allUsers].sort((a, b) => score(b) - score(a));
  }, [allUsers]);

  const visibleAllUsers = useMemo(() => {
    if (canEdit) return sortedAllUsers;
    const managerId = (me?.userId ?? "").trim();
    if (!managerId) return [];
    return sortedAllUsers.filter((u) => u.managerId === managerId);
  }, [canEdit, sortedAllUsers, me?.userId]);

  /**
   * Reporting-to choices for a user row:
   * - Manager (actor): Admins only.
   * - Admin assigning for Member on a team: that team's Managers + all Admins.
   * - Admin assigning for Manager / Member without team: Admins only.
   */
  const reportingToOptionsForTarget = useCallback(
    (target: UserOut): UserOut[] => {
      const admins = allUsers.filter((x) => x.role === "Admin");
      if (isManagerRole && !canEdit) {
        return [...admins].sort(sortUsersByName);
      }
      if (target.role === "Member" && target.teamId) {
        const teamManagers = allUsers.filter(
          (x) => x.role === "Manager" && x.teamId === target.teamId && x.id !== target.id
        );
        const byId = new Map<string, UserOut>();
        for (const x of [...admins, ...teamManagers]) byId.set(x.id, x);
        return [...byId.values()].sort(sortUsersByName);
      }
      return [...admins].sort(sortUsersByName);
    },
    [allUsers, isManagerRole, canEdit]
  );

  const reportingToLabelById = useMemo(() => {
    const map = new Map<string, string>();
    for (const u of allUsers) {
      if (u.role === "Admin" || u.role === "Manager") {
        map.set(u.id, userDisplayLabel(u));
      }
    }
    return map;
  }, [allUsers]);

  const load = useCallback(
    (opts?: { silent?: boolean }) => {
      if (status !== "authenticated") return;
      if (!opts?.silent) {
        setLoading(true);
        setError(null);
      }
      api
        .getMe()
        .catch(() => null)
        .then((meResp) => {
          setMe(
            meResp
              ? {
                  userId: meResp.userId,
                  isAdmin: meResp.isAdmin,
                  role: meResp.role ?? "",
                  department: meResp.department ?? null,
                }
              : null
          );
          return Promise.all([api.getUsers({ role: "Manager" }), api.getTeams(), api.getUsers()]).then(
            ([m, t, u]) => {
              setManagers(m);
              setTeams(t);
              setAllUsers(u);
            }
          );
        })
        .catch(() => {
          if (!opts?.silent) setError("Failed to load data");
        })
        .finally(() => {
          if (!opts?.silent) setLoading(false);
        });
    },
    [status, api]
  );

  const applyEmailCounts = useCallback((counts: { userId: string; emailCount: number }[]) => {
    const byId = new Map(counts.map((c) => [c.userId, c.emailCount]));
    if (byId.size === 0) return;
    setAllUsers((prev) =>
      prev.map((u) => {
        const next = byId.get(u.id);
        return next === undefined || next === u.emailCount ? u : { ...u, emailCount: next };
      })
    );
  }, []);

  const refreshEmailCounts = useCallback(() => {
    if (status !== "authenticated") return;
    api
      .getUserEmailCounts()
      .then(applyEmailCounts)
      .catch(() => {
        /* ignore transient poll errors */
      });
  }, [status, api, applyEmailCounts]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (status !== "authenticated") return;
    refreshEmailCounts();
    emailCountPollRef.current = setInterval(refreshEmailCounts, EMAIL_COUNT_POLL_MS);
    return () => {
      if (emailCountPollRef.current) {
        clearInterval(emailCountPollRef.current);
        emailCountPollRef.current = null;
      }
    };
  }, [status, refreshEmailCounts]);

  /** Refresh roles/teams periodically; email counts use the faster poll above. */
  useEffect(() => {
    if (status !== "authenticated") return;
    const id = window.setInterval(() => load({ silent: true }), 60000);
    return () => window.clearInterval(id);
  }, [status, load]);

  const assignRole = (userId: string, role: string) => {
    if (!canEdit) return;
    setUpdatingId(userId);
    api.updateUser(userId, { role }).then(() => load({ silent: true })).catch(() => setError("Failed to update")).finally(() => setUpdatingId(null));
  };

  const assignTeam = (userId: string, teamId: string) => {
    if (!canEdit) return;
    setUpdatingId(userId);
    api.updateUser(userId, { teamId: teamId || undefined }).then(() => load({ silent: true })).catch(() => setError("Failed to update")).finally(() => setUpdatingId(null));
  };

  const assignReportingTo = (userId: string, managerId: string) => {
    if (!canAssignReportingTo) return;
    setUpdatingId(userId);
    api
      .updateUser(userId, { managerId })
      .then(() => load({ silent: true }))
      .catch(() => setError("Failed to update reporting manager"))
      .finally(() => setUpdatingId(null));
  };

  return (
    <div className="min-w-0 max-w-full space-y-4 sm:space-y-6">
      <div className="min-w-0">
        <h1 className="text-xl font-bold tracking-tight text-neutral-900 dark:text-neutral-50 sm:text-2xl">
          User management
        </h1>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-400">
          <p className="break-words">{error}</p>
        </div>
      )}

      <Card className="min-w-0 rounded-2xl border-border">
        <CardHeader className="p-4 sm:p-6">
          <CardTitle className="flex min-w-0 flex-wrap items-center gap-2 text-base leading-snug">
            <UserCircle className="h-5 w-5 shrink-0 text-indigo-600 dark:text-indigo-400" />
            <span className="min-w-0 break-words">
              Managers
              <span className="tabular-nums text-muted-foreground"> ({managers.length})</span>
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4 pt-0 sm:p-6 sm:pt-0">
          {loading ? (
            <Skeleton className="h-40 w-full rounded-lg sm:h-48" />
          ) : managers.length === 0 ? (
            <p className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">No managers yet.</p>
          ) : (
            <ul className="divide-y divide-neutral-200 dark:divide-neutral-700">
              {managers.map((u) => (
                <li
                  key={u.id}
                  className="flex flex-col gap-3 py-4 first:pt-0 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-words font-medium text-neutral-900 dark:text-neutral-50">
                      {u.displayName ?? u.email}
                    </p>
                    <p className="break-words text-xs text-neutral-500 dark:text-neutral-400">{u.email}</p>
                    <p className="mt-1 break-words text-sm text-neutral-600 dark:text-neutral-300">
                      {u.teamName ?? "No team"} · {u.reportCount} report{u.reportCount !== 1 ? "s" : ""}
                    </p>
                  </div>
                  <div className="w-full border-t border-neutral-100 pt-3 dark:border-neutral-800 sm:w-auto sm:shrink-0 sm:border-t-0 sm:pt-0">
                    <Select
                      value={u.teamId ?? ""}
                      onValueChange={(tid) => assignTeam(u.id, tid)}
                      disabled={!!updatingId || !canEdit}
                    >
                      <SelectTrigger className="h-10 w-full min-w-0 sm:w-[160px]">
                        <SelectValue placeholder="Team" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="">No team</SelectItem>
                        {teams.map((t) => (
                          <SelectItem key={t.id} value={t.id}>
                            {t.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card className="min-w-0 rounded-2xl border-border">
        <CardHeader className="p-4 sm:p-6">
          <CardTitle className="flex min-w-0 flex-wrap items-start gap-2 text-base leading-snug">
            <Users className="mt-0.5 h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
            <span className="min-w-0 break-words">
              {canEdit
                ? "All users - assign role or team"
                : "Members assigned to me"}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4 pt-0 sm:p-6 sm:pt-0">
          {loading ? null : visibleAllUsers.length === 0 ? (
            <p className="py-4 text-sm text-neutral-500 dark:text-neutral-400">
              No users. Create users via API or seed.
            </p>
          ) : (
            <ul className="divide-y divide-neutral-200 dark:divide-neutral-700">
              {visibleAllUsers.map((u) => (
                <li
                  key={u.id}
                  className="flex flex-col gap-3 py-3 first:pt-0 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-words font-medium text-neutral-900 dark:text-neutral-50">
                      {u.displayName ?? u.email}
                    </p>
                    <p className="break-words text-xs text-neutral-500 dark:text-neutral-400">
                      {u.email} · {u.teamName ?? "-"}
                    </p>
                    <p className="mt-1 break-words text-xs leading-relaxed text-neutral-500 dark:text-neutral-400">
                      Emails: <span className="tabular-nums font-medium text-neutral-700 dark:text-neutral-300">{formatEmailCount(u.emailCount)}</span> · Joined:{" "}
                      <span className="tabular-nums">{formatActivity(u.createdAt)}</span>
                    </p>
                  </div>
                  <div className="flex w-full min-w-0 flex-col gap-2 border-t border-neutral-100 pt-3 dark:border-neutral-800 sm:w-auto sm:shrink-0 sm:flex-row sm:flex-wrap sm:border-t-0 sm:pt-0">
                    <Select
                      value={u.role}
                      onValueChange={(role) => assignRole(u.id, role)}
                      disabled={!!updatingId || !canEdit}
                    >
                      <SelectTrigger className="h-10 w-full min-w-0 sm:w-[110px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="Member">Member</SelectItem>
                        <SelectItem value="Manager">Manager</SelectItem>
                        <SelectItem value="Admin">Admin</SelectItem>
                      </SelectContent>
                    </Select>
                    <Select
                      value={u.teamId ?? ""}
                      onValueChange={(tid) => assignTeam(u.id, tid)}
                      disabled={!!updatingId || !canEdit}
                    >
                      <SelectTrigger className="h-10 w-full min-w-0 sm:w-[140px]">
                        <SelectValue placeholder="Team" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="">No team</SelectItem>
                        {teams.map((t) => (
                          <SelectItem key={t.id} value={t.id}>
                            {t.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Select
                      value={u.managerId ?? ""}
                      onValueChange={(mid) => assignReportingTo(u.id, mid)}
                      disabled={!!updatingId || !canAssignReportingTo}
                    >
                      <SelectTrigger className="h-10 w-full min-w-0 sm:w-[180px]" aria-label="Reporting to">
                        <SelectValue placeholder="Reporting to" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="">No manager</SelectItem>
                        {reportingToOptionsForTarget(u).map((m) => (
                            <SelectItem key={m.id} value={m.id}>
                              {userDisplayLabel(m)}
                              {m.role === "Manager" ? " (Manager)" : ""}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                    {!canAssignReportingTo && u.managerId && (
                      <p className="text-xs text-neutral-500 dark:text-neutral-400 sm:hidden">
                        Reporting to: {reportingToLabelById.get(u.managerId) ?? "-"}
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
