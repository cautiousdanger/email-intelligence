import type { MomRecord } from "@/lib/mom-storage";
import type {
  HealthResponse,
  DashboardMetrics,
  CalendarEventsResponse,
  NotificationsResponse,
  MyProjectsResponse,
  FollowUpTrackerResponse,
  FollowUpRemindersResponse,
  FollowUpTrackerHistoryResponse,
  EmailsResponse,
  EmailDetail,
  ConversationsResponse,
  ThreadEmailsResponse,
  QueueStatusResponse,
  SettingsConfig,
  SystemHealthResponse,
  EscalationsResponse,
  LeadsResponse,
  TeamOut,
  UserOut,
  UserEmailCountOut,
  UserEscalationCountOut,
  UserLeadCountOut,
  WorkflowNode,
  TeamStatusOut,
  MeResponse,
  RecentSignInOut,
  TeamProjectOut,
  ProjectAssignmentUpsert,
  RetaggedResponse,
  TrackerDashboardResponse,
  ProjectTrackerRow,
  TrackerProjectEmailsResponse,
  ReviewEscalationUser,
  ReviewLeadUser,
  ReviewProjectTrackerUser,
  RetagActionResponse,
  RetagApprovalOut,
  MyRetagRequestsResponse,
  DailyBulkSummariesResponse,
  DailyBulkSummaryGenerateResponse,
} from "@/lib/types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/** Parse FastAPI `{ detail: string | object[] }` or `{ error: string }` into a user-facing message. */
function apiErrorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const o = body as { detail?: unknown; error?: string; message?: string };
    if (typeof o.error === "string" && o.error.trim()) return o.error;
    if (typeof o.message === "string" && o.message.trim()) return o.message;
    const d = o.detail;
    if (typeof d === "string" && d.trim()) return d;
    if (Array.isArray(d) && d.length > 0) {
      const first = d[0] as { msg?: string };
      if (typeof first?.msg === "string") return first.msg;
    }
  }
  return `API error: ${status}`;
}

function buildHeaders(
  userEmail: string | null,
  userDisplayName?: string | null,
  extra?: HeadersInit
): HeadersInit {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (userEmail?.trim()) headers["X-User-Email"] = userEmail.trim();
  if (userDisplayName?.trim()) headers["X-User-Name"] = userDisplayName.trim();
  return { ...headers, ...(extra as Record<string, string>) };
}

async function fetchApi<T>(
  path: string,
  options?: RequestInit,
  userEmail?: string | null,
  userDisplayName?: string | null
): Promise<T> {
  if (!API_BASE) {
    throw new Error(
      "NEXT_PUBLIC_API_URL is not set. Configure it (e.g. http://localhost:8000) to use the real backend APIs."
    );
  }
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: buildHeaders(userEmail ?? null, userDisplayName ?? null, options?.headers),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(body, res.status));
  }
  return res.json() as Promise<T>;
}

async function fetchBlob(
  path: string,
  options: RequestInit | undefined,
  userEmail: string | null,
  userDisplayName?: string | null
): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: buildHeaders(userEmail ?? null, userDisplayName ?? null, {
      ...(options?.headers as Record<string, string>),
      Accept: "text/csv",
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(body, res.status));
  }
  return res.blob();
}

/**
 * URL for email attachments. Uses the Next.js proxy path so the server can add X-User-Email from session
 * (browser cannot send custom headers for direct backend attachment requests).
 */
export function getAttachmentUrl(emailId: string, attachmentId: string, download = false): string {
  const params = download ? "?download=1" : "";
  return `/api/emails/${emailId}/attachments/${attachmentId}${params}`;
}

function createApi(userEmail: string | null, userDisplayName?: string | null) {
  const withUser = <T>(path: string, options?: RequestInit) =>
    fetchApi<T>(path, options, userEmail, userDisplayName);
  return {
    getHealth: () => withUser<HealthResponse>("/api/health"),
    getMomRecords: () => withUser<{ records: MomRecord[] }>("/api/mom/records"),
    upsertMomRecord: (body: MomRecord) =>
      withUser<{ ok: boolean }>("/api/mom/records", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    getDashboardMetrics: (period?: "daily" | "weekly" | "monthly" | "yearly") =>
      withUser<DashboardMetrics>(`/api/dashboard/metrics${period ? `?period=${encodeURIComponent(period)}` : ""}`),
    getDailyBulkSummaries: () =>
      withUser<DailyBulkSummariesResponse>("/api/dashboard/daily-bulk-summaries"),
    generateDailyBulkSummary: (date: string) =>
      withUser<DailyBulkSummaryGenerateResponse>(
        `/api/dashboard/daily-bulk-summaries/${encodeURIComponent(date)}/generate`,
        { method: "POST" }
      ),
    /**
     * Dashboard calendar list. Default: meeting invites from synced Mail (`source=mail`, no Calendars.Read).
     * Optional `source=graph` + Bearer for legacy Graph calendarView.
     */
    getDashboardCalendarEvents: (
      days?: number,
      accessToken?: string | null,
      source: "mail" | "graph" = "mail"
    ) => {
      const params = new URLSearchParams();
      if (days != null) params.set("days", String(days));
      if (source) params.set("source", source);
      const q = params.toString();
      const headers =
        accessToken?.trim() != null && accessToken.trim().length > 0
          ? { ...buildHeaders(userEmail ?? null, userDisplayName ?? null), Authorization: `Bearer ${accessToken.trim()}` }
          : buildHeaders(userEmail ?? null, userDisplayName ?? null);
      return fetchApi<CalendarEventsResponse>(
        `/api/dashboard/calendar-events${q ? `?${q}` : ""}`,
        { headers },
        userEmail ?? null,
        userDisplayName ?? null
      );
    },
    getNotifications: () => withUser<NotificationsResponse>("/api/dashboard/notifications"),
    getMyProjects: () => withUser<MyProjectsResponse>("/api/dashboard/my-projects"),
    getFollowUpTracker: () => withUser<FollowUpTrackerResponse>("/api/dashboard/follow-up/tracker"),
    getFollowUpReminders: () => withUser<FollowUpRemindersResponse>("/api/dashboard/follow-up/reminders"),
    getFollowUpTrackerHistory: (projectId: string, days?: number) => {
      const search = new URLSearchParams({ projectId });
      if (days != null) search.set("days", String(days));
      return withUser<FollowUpTrackerHistoryResponse>(`/api/dashboard/follow-up/tracker/history?${search.toString()}`);
    },
    getEmails: (params?: {
      page?: number;
      pageSize?: number;
      search?: string;
      from?: string;
      to?: string;
      category?: string;
      priorityLabel?: string;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.search) searchParams.set("search", params.search);
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.category) searchParams.set("category", params.category);
      if (params?.priorityLabel) searchParams.set("priorityLabel", params.priorityLabel);
      const q = searchParams.toString();
      return withUser<EmailsResponse>(`/api/emails${q ? `?${q}` : ""}`);
    },
    /** Admin-only: all mailboxes; set deletedOnly=true for user soft-deleted messages */
    getAdminEmails: (params?: {
      page?: number;
      pageSize?: number;
      search?: string;
      from?: string;
      to?: string;
      category?: string;
      deletedOnly?: boolean;
      /** Cross-domain: sender or any recipient not on company internal domain (backend setting). */
      externalParticipants?: boolean;
      /** From address outside company internal domain (e.g. not @cachedigitech.com). */
      externalSendersOnly?: boolean;
      /** sent | received - from synced folder (Sent vs non-Sent). */
      mailDirection?: "sent" | "received";
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.search) searchParams.set("search", params.search);
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.category) searchParams.set("category", params.category);
      if (params?.deletedOnly === true) searchParams.set("deletedOnly", "true");
      if (params?.externalParticipants === true) searchParams.set("externalParticipants", "true");
      if (params?.externalSendersOnly === true) searchParams.set("externalSendersOnly", "true");
      if (params?.mailDirection === "sent" || params?.mailDirection === "received") {
        searchParams.set("mailDirection", params.mailDirection);
      }
      const q = searchParams.toString();
      return withUser<EmailsResponse>(`/api/admin/emails${q ? `?${q}` : ""}`);
    },
    restoreAdminEmail: (emailId: string) =>
      withUser<{ ok: boolean; emailId?: string }>(`/api/admin/emails/${emailId}/restore`, { method: "POST" }),
    /** Enqueue Microsoft Graph Deleted Items sync for every registered user mailbox (admin). */
    syncOutlookDeleted: (body?: { days?: number }) =>
      withUser<{ ok: boolean; taskId?: string; message?: string }>("/api/admin/sync-outlook-deleted", {
        method: "POST",
        body: JSON.stringify(body ?? {}),
      }),
    softDeleteEmail: (emailId: string) =>
      withUser<{ ok: boolean; emailId?: string }>(`/api/emails/${emailId}/soft-delete`, { method: "POST" }),
    getEmail: (id: string) => withUser<EmailDetail>(`/api/emails/${id}`),
    /** List email threads (conversations) for Threads view */
    getConversations: (params?: { page?: number; pageSize?: number; search?: string }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.search) searchParams.set("search", params.search);
      const q = searchParams.toString();
      return withUser<ConversationsResponse>(`/api/emails/conversations${q ? `?${q}` : ""}`);
    },
    /** Get all emails in a thread (chronological) */
    getConversationEmails: (conversationId: string) =>
      withUser<ThreadEmailsResponse>(`/api/emails/conversations/${encodeURIComponent(conversationId)}/emails`),
    /** CSV export: reply rows with response time, subjects, From/To/Cc/Bcc (date range inclusive). */
    downloadThreadRepliesCsv: (params?: { from?: string; to?: string; conversationId?: string }) => {
      const searchParams = new URLSearchParams();
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.conversationId) searchParams.set("conversationId", params.conversationId);
      const q = searchParams.toString();
      return fetchBlob(
        `/api/emails/conversations/replies-export${q ? `?${q}` : ""}`,
        { method: "GET" },
        userEmail,
        userDisplayName
      );
    },
    /** Backfill conversationId from Graph so Threads view populates (no re-login needed) */
    backfillConversationIds: (limit?: number) =>
      withUser<{ ok: boolean; updated?: number; message?: string; error?: string }>(
        `/api/emails/backfill-conversation-ids${limit != null ? `?limit=${limit}` : ""}`,
        { method: "POST" }
      ),
    getAttachmentUrl,
    getQueueStatus: () => withUser<QueueStatusResponse>("/api/queue/status"),
    getSettings: () => withUser<SettingsConfig>("/api/settings"),
    triggerBackfill: (body?: { user_id?: string; folder_id?: string; days?: number; all?: boolean; from_date?: string; to_date?: string }) =>
      withUser<{ ok: boolean; taskId?: string; userId?: string; message?: string; error?: string }>(
        "/api/emails/backfill",
        { method: "POST", body: JSON.stringify(body ?? {}) }
      ),
    triggerClassifyBackfill: (body?: { limit?: number }) =>
      withUser<{ ok: boolean; taskId?: string; message?: string; error?: string }>(
        "/api/emails/classify-backfill",
        { method: "POST", body: JSON.stringify(body ?? {}) }
      ),
    getClassificationBatchStatus: (since: string) =>
      withUser<{ classifiedSinceCount: number; latestAiProcessedAt: string | null }>(
        `/api/emails/classification-batch-status?since=${encodeURIComponent(since)}`
      ),
    postClassificationBatchSummary: (body: { since: string }) =>
      withUser<{ ok: boolean; count: number; summary: string | null; error?: string }>(
        "/api/emails/classification-batch-summary",
        { method: "POST", body: JSON.stringify(body) }
      ),
    retryAi: (emailId: string) =>
      withUser<{ ok: boolean; message?: string; emailId?: string }>(`/api/emails/${emailId}/retry-ai`, { method: "POST" }),
    generateSummary: (emailId: string) =>
      withUser<{ ok: boolean; message?: string; emailId?: string }>(`/api/emails/${emailId}/generate-summary`, { method: "POST" }),
    getSystemHealth: () => withUser<SystemHealthResponse>("/api/system/health"),
    // Phase 3 - Escalations & Leads
    getEscalations: (params?: {
      page?: number;
      pageSize?: number;
      from?: string;
      to?: string;
      team?: string;
      mine?: boolean;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.team) searchParams.set("team", params.team);
      if (params?.mine === true) searchParams.set("mine", "true");
      const q = searchParams.toString();
      return withUser<EscalationsResponse>(`/api/escalations${q ? `?${q}` : ""}`);
    },
    /** Admin: list users with escalation count (for escalations-by-user view). */
    getEscalationCountsByUser: () =>
      withUser<UserEscalationCountOut[]>("/api/admin/escalation-counts"),
    /** Admin: list escalations for a specific user's mailbox. */
    getAdminEscalationsForUser: (params: {
      mailbox: string;
      page?: number;
      pageSize?: number;
      from?: string;
      to?: string;
      team?: string;
    }) => {
      const searchParams = new URLSearchParams();
      searchParams.set("mailbox", params.mailbox);
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.team) searchParams.set("team", params.team);
      return withUser<EscalationsResponse>(`/api/admin/escalations?${searchParams.toString()}`);
    },
    getLeads: (params?: {
      page?: number;
      pageSize?: number;
      label?: string;
      team?: string;
      from?: string;
      to?: string;
      mine?: boolean;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.label) searchParams.set("label", params.label);
      if (params?.team) searchParams.set("team", params.team);
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.mine === true) searchParams.set("mine", "true");
      const q = searchParams.toString();
      return withUser<LeadsResponse>(`/api/leads${q ? `?${q}` : ""}`);
    },
    /** Admin: list users with lead count (for leads-by-user view). */
    getLeadCountsByUser: () =>
      withUser<UserLeadCountOut[]>("/api/admin/lead-counts"),
    /** Admin: list leads for a specific user's mailbox. */
    getAdminLeadsForUser: (params: {
      mailbox: string;
      page?: number;
      pageSize?: number;
      label?: string;
      from?: string;
      to?: string;
      team?: string;
    }) => {
      const searchParams = new URLSearchParams();
      searchParams.set("mailbox", params.mailbox);
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.label) searchParams.set("label", params.label);
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      if (params?.team) searchParams.set("team", params.team);
      return withUser<LeadsResponse>(`/api/admin/leads?${searchParams.toString()}`);
    },
    assignEmailToTeam: (emailId: string, team: string) =>
      withUser<{ ok: boolean; emailId: string; assignedTeam: string | null }>(
        `/api/emails/${emailId}/assign?team=${encodeURIComponent(team)}`,
        { method: "PATCH" }
      ),
    /** Retag: clear escalation/lead, assign department (mailbox owner only). */
    retagEmail: (emailId: string, assignedTeam: string) =>
      withUser<RetagActionResponse>(`/api/emails/${emailId}/retag`, {
        method: "PATCH",
        body: JSON.stringify({ assignedTeam }),
      }),
    /** Departments for retag dropdown (signed-in user). */
    getRetagDepartmentOptions: () =>
      withUser<{ departments: string[] }>("/api/retag/department-options"),
    getRetagged: (params?: { page?: number; pageSize?: number; from?: string; to?: string }) => {
      const searchParams = new URLSearchParams();
      searchParams.set("mine", "true");
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params?.from) searchParams.set("from", params.from);
      if (params?.to) searchParams.set("to", params.to);
      return withUser<RetaggedResponse>(`/api/retagged?${searchParams.toString()}`);
    },
    getMyRetagRequests: (params?: { page?: number; pageSize?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      const q = searchParams.toString();
      return withUser<MyRetagRequestsResponse>(`/api/retag-requests/mine${q ? `?${q}` : ""}`);
    },
    /** Admin: retag mail in another user's mailbox */
    retagEmailAdmin: (emailId: string, mailbox: string, assignedTeam: string) =>
      withUser<RetagActionResponse>(
        `/api/admin/emails/${emailId}/retag?mailbox=${encodeURIComponent(mailbox)}`,
        { method: "PATCH", body: JSON.stringify({ assignedTeam }) }
      ),
    getAdminRetagged: (params: { mailbox: string; page?: number; pageSize?: number; from?: string; to?: string }) => {
      const searchParams = new URLSearchParams();
      searchParams.set("mailbox", params.mailbox);
      if (params.page != null) searchParams.set("page", String(params.page));
      if (params.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      if (params.from) searchParams.set("from", params.from);
      if (params.to) searchParams.set("to", params.to);
      return withUser<RetaggedResponse>(`/api/admin/retagged?${searchParams.toString()}`);
    },
    getRetagApprovals: (params?: { status?: "pending" | "approved" | "rejected"; page?: number; pageSize?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.status) searchParams.set("status", params.status);
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      const q = searchParams.toString();
      return withUser<RetagApprovalOut[]>(`/api/admin/retag-approvals${q ? `?${q}` : ""}`);
    },
    approveRetagRequest: (requestId: string) =>
      withUser<{ ok: boolean; requestId: string; status: string }>(
        `/api/admin/retag-approvals/${encodeURIComponent(requestId)}/approve`,
        { method: "POST" }
      ),
    rejectRetagRequest: (requestId: string, reviewNote?: string) => {
      const search = new URLSearchParams();
      if (reviewNote?.trim()) search.set("reviewNote", reviewNote.trim());
      const q = search.toString();
      return withUser<{ ok: boolean; requestId: string; status: string }>(
        `/api/admin/retag-approvals/${encodeURIComponent(requestId)}/reject${q ? `?${q}` : ""}`,
        { method: "POST" }
      );
    },
    // Phase 4 - Admin (requires admin role)
    getMe: () => withUser<MeResponse>("/api/me"),
    recordLogout: () => withUser<{ ok: boolean }>("/api/me/logout", { method: "POST" }),
    dismissRolePromotion: () =>
      withUser<{ ok: boolean }>("/api/me/dismiss-role-promotion", { method: "POST" }),
    getTeams: () => withUser<TeamOut[]>("/api/admin/teams"),
    getTeam: (teamId: string) => withUser<TeamOut>(`/api/admin/teams/${teamId}`),
    getTeamStatus: (teamId: string) => withUser<TeamStatusOut>(`/api/admin/teams/${teamId}/status`),
    getUsers: (params?: { role?: string; teamId?: string }) => {
      const searchParams = new URLSearchParams();
      if (params?.role) searchParams.set("role", params.role);
      if (params?.teamId) searchParams.set("teamId", params.teamId);
      const q = searchParams.toString();
      return withUser<UserOut[]>(`/api/admin/users${q ? `?${q}` : ""}`);
    },
    getUserEmailCounts: () => withUser<UserEmailCountOut[]>("/api/admin/users/email-counts"),
    getRecentSignIns: (params?: { limit?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.limit != null) searchParams.set("limit", String(params.limit));
      const q = searchParams.toString();
      return withUser<RecentSignInOut[]>(`/api/admin/recent-sign-ins${q ? `?${q}` : ""}`);
    },
    getWorkflow: () => withUser<WorkflowNode[]>("/api/admin/workflow"),
    updateUser: (userId: string, data: { role?: string; teamId?: string; managerId?: string; isTeamLead?: boolean }) => {
      const searchParams = new URLSearchParams();
      if (data.role != null) searchParams.set("role", data.role);
      if (data.teamId != null) searchParams.set("teamId", data.teamId);
      // Include managerId when present (use "" for "No manager" so backend clears it)
      if (data.managerId !== undefined) searchParams.set("managerId", data.managerId);
      if (data.isTeamLead != null) searchParams.set("isTeamLead", String(data.isTeamLead));
      const q = searchParams.toString();
      return withUser<{ ok: boolean; userId: string }>(`/api/admin/users/${userId}?${q}`, { method: "PATCH" });
    },
    createUser: (params: { email: string; displayName?: string; role?: string; teamId?: string }) => {
      const searchParams = new URLSearchParams();
      searchParams.set("email", params.email);
      if (params.displayName) searchParams.set("displayName", params.displayName);
      if (params.role) searchParams.set("role", params.role);
      if (params.teamId) searchParams.set("teamId", params.teamId);
      return withUser<{ ok: boolean; userId: string; email: string }>(
        `/api/admin/users?${searchParams.toString()}`,
        { method: "POST" }
      );
    },
    getProjectsWorkflow: (params?: { teamId?: string; status?: "running" | "new" | "planned" | "completed" }) => {
      const searchParams = new URLSearchParams();
      if (params?.teamId) searchParams.set("teamId", params.teamId);
      if (params?.status) searchParams.set("status", params.status);
      const q = searchParams.toString();
      return withUser<TeamProjectOut[]>(`/api/admin/projects-workflow${q ? `?${q}` : ""}`);
    },
    getProjectWorkflow: (projectId: string) =>
      withUser<TeamProjectOut>(`/api/admin/projects-workflow/${encodeURIComponent(projectId)}`),
    /** Threads in the creating admin's mailbox related to this project (inbox/spam/junk seeds; full reply chain). */
    getProjectMailboxThreads: (projectId: string, params?: { page?: number; pageSize?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.page != null) searchParams.set("page", String(params.page));
      if (params?.pageSize != null) searchParams.set("pageSize", String(params.pageSize));
      const q = searchParams.toString();
      return withUser<ConversationsResponse>(
        `/api/admin/projects-workflow/${encodeURIComponent(projectId)}/mailbox-threads${q ? `?${q}` : ""}`
      );
    },
    /** Only messages that contain the project name (subject/body). */
    getProjectMailboxThreadEmails: (projectId: string, conversationId: string) =>
      withUser<ThreadEmailsResponse>(
        `/api/admin/projects-workflow/${encodeURIComponent(projectId)}/mailbox-threads/${encodeURIComponent(conversationId)}/emails`
      ),
    createProjectWorkflow: (body: {
      name: string;
      teamId?: string | null;
      status?: "running" | "new" | "planned" | "completed";
      structure?: { phases?: string[]; notes?: string };
      projectLeadUserId?: string | null;
      assignments?: ProjectAssignmentUpsert[];
      assignedUserIds?: string[];
    }) =>
      withUser<TeamProjectOut>("/api/admin/projects-workflow", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    updateProjectWorkflow: (
      projectId: string,
      body: {
        name: string;
        teamId?: string | null;
        status?: "running" | "new" | "planned" | "completed";
        structure?: { phases?: string[]; notes?: string };
        projectLeadUserId?: string | null;
        assignments?: ProjectAssignmentUpsert[];
        assignedUserIds?: string[];
      }
    ) =>
      withUser<TeamProjectOut>(`/api/admin/projects-workflow/${projectId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    getAdminTracker: () => withUser<TrackerDashboardResponse>("/api/admin/tracker"),
    patchAdminTrackerSchedule: (
      projectId: string,
      scheduleDays: string[],
      memberDeadlineBeforeDays?: Record<string, string | null>,
      memberScheduleDays?: Record<string, string[] | null>
    ) =>
      withUser<ProjectTrackerRow>(`/api/admin/tracker/${encodeURIComponent(projectId)}`, {
        method: "PATCH",
        body: JSON.stringify({
          scheduleDays,
          ...(memberDeadlineBeforeDays != null ? { memberDeadlineBeforeDays } : {}),
          ...(memberScheduleDays != null ? { memberScheduleDays } : {}),
        }),
      }),
    getAdminTrackerProjectEmails: (projectId: string, params?: { days?: number; limit?: number }) => {
      const search = new URLSearchParams();
      if (params?.days != null) search.set("days", String(params.days));
      if (params?.limit != null) search.set("limit", String(params.limit));
      const q = search.toString();
      return withUser<TrackerProjectEmailsResponse>(
        `/api/admin/tracker/${encodeURIComponent(projectId)}/emails${q ? `?${q}` : ""}`
      );
    },
    getAdminReviewEscalationReplies: (days = 30) =>
      withUser<ReviewEscalationUser[]>(`/api/admin/review/escalation-replies?days=${encodeURIComponent(String(days))}`),
    getAdminReviewLeadReplies: (days = 30) =>
      withUser<ReviewLeadUser[]>(`/api/admin/review/lead-replies?days=${encodeURIComponent(String(days))}`),
    getAdminReviewProjectTracker: (days = 30) =>
      withUser<ReviewProjectTrackerUser[]>(`/api/admin/review/project-tracker?days=${encodeURIComponent(String(days))}`),
  };
}

/** API client scoped to the given user (session email and optional display name). Pass null when unauthenticated; backend will return 401. */
export function getApi(userEmail: string | null, userDisplayName?: string | null) {
  return createApi(userEmail, userDisplayName);
}

/** Legacy default api (no user header). Use getApi(session?.user?.email, session?.user?.name) in app code for per-user data. */
export const api = createApi(null);
