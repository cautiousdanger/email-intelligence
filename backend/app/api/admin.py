"""
Phase 4: Admin APIs - teams, users, workflow (who leads whom), team status.
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query, HTTPException, Body
from sqlalchemy import exists, func, or_, desc, text, not_
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import OperationalError, ProgrammingError
from pydantic import BaseModel, Field

from app.db.session import get_db
from app.db.models import Team, User, Email, Attachment, TeamProject, ProjectAssignment, RetagApprovalRequest
from app.api.admin_access import (
    actor_manager_scope_mailboxes,
    assert_mailbox_in_manager_scope,
    is_admin_actor as _is_admin_actor,
    manager_actor_row as _manager_actor_row,
)
from app.api.deps import get_admin_user, get_admin_or_manager_user, get_current_user_email
from app.api.phase3 import _email_to_item, RetagEmailBody, _perform_retag, apply_received_at_date_range_filter
from app.api.emails import (
    ConversationOut,
    ConversationsResponse,
    EmailDetailOut,
    EmailOut,
    EmailsResponse,
    AttachmentOut,
    ThreadEmailsResponse,
    _display_category,
    _bcc_from_email,
)
from app.config import get_settings

router = APIRouter()


def _admin_sent_folder_clause():
    """Sent Items: well-known sync used 'Sent'; full-folder sync uses Outlook displayName (e.g. Sent Items)."""
    fn = func.lower(func.coalesce(Email.folder_name, ""))
    return or_(fn == "sent", fn == "sent items", Email.folder_name.ilike("sent items%"))


def _external_participant_sql_filter(internal_domain: str):
    """
    Postgres: at least one of sender / to / cc / bcc has an @domain where domain != internal_domain.
    Table name must match emails.__tablename__.
    """
    d = (internal_domain or "").strip().lower().lstrip("@")
    if not d:
        return None
    return text(
        """
        (
          (strpos(coalesce(lower(emails.sender_email), ''), '@') > 0
            AND lower(split_part(emails.sender_email, '@', 2)) != :domain)
          OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
              coalesce(emails.to_recipients, '[]'::jsonb)
              || coalesce(emails.cc_recipients, '[]'::jsonb)
              || coalesce(emails.bcc_recipients, '[]'::jsonb)
            ) AS addr
            WHERE (addr->>'email') IS NOT NULL
              AND strpos(lower(addr->>'email'), '@') > 0
              AND lower(split_part(addr->>'email', '@', 2)) != :domain
          )
        )
        """
    ).bindparams(domain=d)


# Mailboxes to exclude from "Users - escalation count" / "Users - lead count" (e.g. default backfill mailbox already on admin dashboard)
def _excluded_mailboxes_for_user_lists() -> set[str]:
    s = get_settings()
    excluded = set()
    if getattr(s, "mailbox_email", None) and str(s.mailbox_email).strip():
        excluded.add(str(s.mailbox_email).strip().lower())
    # Always exclude techbank (default backfill mailbox) so it does not appear in per-user list
    excluded.add("techbank@cachedigitech.com")
    return excluded


# --- Schemas ---
class TeamOut(BaseModel):
    id: str
    name: str
    slug: str | None
    memberCount: int = 0

    model_config = {"from_attributes": True}


class UserOut(BaseModel):
    id: str
    email: str
    displayName: str | None = Field(None, alias="displayName")
    role: str
    teamId: str | None = Field(None, alias="teamId")
    teamName: str | None = Field(None, alias="teamName")
    managerId: str | None = Field(None, alias="managerId")
    isTeamLead: bool = Field(False, alias="isTeamLead")
    reportCount: int = 0
    emailCount: int = Field(0, alias="emailCount")
    lastLoginAt: datetime | None = None
    createdAt: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class UserEmailCountOut(BaseModel):
    userId: str = Field(alias="userId")
    email: str
    emailCount: int = Field(0, alias="emailCount")

    model_config = {"populate_by_name": True}


class WorkflowNode(BaseModel):
    id: str
    email: str
    displayName: str | None
    role: str
    teamName: str | None
    isTeamLead: bool
    managerId: str | None
    reportIds: list[str] = []
    """Team project names this user is assigned to (admin team projects)."""
    projectNames: list[str] = Field(default_factory=list)


class ProjectAssignmentOut(BaseModel):
    userId: str
    email: str
    displayName: str | None = None
    role: str | None = None  # project-specific role
    responsibilities: str | None = None  # what they do on this project
    # Project-internal manager (another assignee), not org workflow.
    reportsToUserId: str | None = None


class ProjectAssignmentUpsertIn(BaseModel):
    userId: str
    role: str | None = None
    responsibilities: str | None = None
    reportsToUserId: str | None = None


class TeamProjectOut(BaseModel):
    id: str
    name: str
    teamId: str | None = None
    teamName: str | None = None
    status: str
    structure: dict | None = None
    # Explicit project lead (not org "team lead").
    projectLeadUserId: str | None = None
    # Admin user id who created the project (used for mailbox thread access).
    createdByUserId: str | None = None
    assignedUsers: list[ProjectAssignmentOut] = []
    createdAt: datetime
    updatedAt: datetime


class TeamProjectUpsertIn(BaseModel):
    name: str
    teamId: str | None = None
    status: str = "running"
    structure: dict | None = None
    projectLeadUserId: str | None = None
    # Per-user project role and responsibilities (preferred over assignedUserIds alone).
    assignments: list[ProjectAssignmentUpsertIn] = Field(default_factory=list)
    # Legacy: user ids only. Ignored when assignments is non-empty.
    assignedUserIds: list[str] = Field(default_factory=list)


class TeamStatusOut(BaseModel):
    teamId: str
    teamName: str
    emailsAssigned: int
    escalationsCount: int
    leadsCount: int


class UserEscalationCountOut(BaseModel):
    """User with count of escalation emails in their mailbox (for admin escalations-by-user list)."""
    email: str
    displayName: str | None = Field(None, alias="displayName")
    escalationCount: int = Field(0, alias="escalationCount")
    readCount: int = Field(0, alias="readCount")
    unreadCount: int = Field(0, alias="unreadCount")
    repliedCount: int = Field(0, alias="repliedCount")

    model_config = {"populate_by_name": True}


class UserLeadCountOut(BaseModel):
    """User with count of lead emails in their mailbox (for admin leads-by-user list)."""
    email: str
    displayName: str | None = Field(None, alias="displayName")
    leadCount: int = Field(0, alias="leadCount")
    readCount: int = Field(0, alias="readCount")
    unreadCount: int = Field(0, alias="unreadCount")
    repliedCount: int = Field(0, alias="repliedCount")

    model_config = {"populate_by_name": True}


class RecentSignInOut(BaseModel):
    userId: str
    email: str
    displayName: str | None = None
    role: str
    lastLoginAt: datetime | None = None
    createdAt: datetime | None = None

    model_config = {"populate_by_name": True}


class RetagApprovalOut(BaseModel):
    id: str
    emailId: str
    mailboxOwnerEmail: str
    requestedByEmail: str
    requestedTeam: str
    status: str
    requestedAt: datetime
    reviewedAt: datetime | None = None
    reviewedByEmail: str | None = None
    reviewNote: str | None = None
    emailSubject: str | None = None
    sender: str | None = None
    receivedAt: datetime | None = None

    model_config = {"populate_by_name": True}


def _serialize_project(db: Session, project: TeamProject) -> TeamProjectOut:
    team_name = project.team.name if getattr(project, "team", None) else None
    assignments = (
        db.query(ProjectAssignment, User)
        .join(User, User.id == ProjectAssignment.user_id)
        .filter(ProjectAssignment.project_id == project.id)
        .order_by(User.email)
        .all()
    )
    users = [
        ProjectAssignmentOut(
            userId=u.id,
            email=u.email,
            displayName=u.display_name,
            role=a.role,
            responsibilities=a.responsibilities,
            reportsToUserId=a.reports_to_user_id,
        )
        for a, u in assignments
    ]
    return TeamProjectOut(
        id=project.id,
        name=project.name,
        teamId=project.team_id,
        teamName=team_name,
        status=project.status,
        structure=project.structure if isinstance(project.structure, dict) else None,
        projectLeadUserId=project.project_lead_user_id,
        createdByUserId=project.created_by_user_id,
        assignedUsers=users,
        createdAt=project.created_at,
        updatedAt=project.updated_at,
    )


def _effective_project_assignments(payload: TeamProjectUpsertIn) -> list[ProjectAssignmentUpsertIn]:
    if payload.assignments:
        return payload.assignments
    return [
        ProjectAssignmentUpsertIn(userId=uid.strip())
        for uid in (payload.assignedUserIds or [])
        if isinstance(uid, str) and uid.strip()
    ]


def _assignment_valid_user_ids(items: list[ProjectAssignmentUpsertIn], db: Session) -> set[str]:
    uids = list({(i.userId or "").strip() for i in items if (i.userId or "").strip()})
    if not uids:
        return set()
    users = db.query(User).filter(User.id.in_(uids)).all()
    return {u.id for u in users}


def _normalize_project_reports_to(uid: str, reports_to: str | None, valid: set[str]) -> str | None:
    if not reports_to or not str(reports_to).strip():
        return None
    r = str(reports_to).strip()
    if r == uid or r not in valid:
        return None
    return r


def _resolve_project_lead_for_save(
    lead_id: str | None, valid_assignees: set[str]
) -> str | None:
    raw = (lead_id or "").strip() or None
    if not raw:
        return None
    if not valid_assignees:
        raise HTTPException(
            status_code=400,
            detail="Cannot set a project lead when no users are assigned.",
        )
    if raw not in valid_assignees:
        raise HTTPException(
            status_code=400,
            detail="Project lead must be one of the assigned users.",
        )
    return raw


def _user_id_for_admin_email(db: Session, admin_email: str) -> str | None:
    u = db.query(User).filter(User.email == (admin_email or "").strip().lower()).first()
    return u.id if u else None


def _folder_is_inbox_spam_junk(email: Email) -> bool:
    fn = (email.folder_name or "").lower()
    fid = (email.folder_id or "").lower()
    for k in ("inbox", "spam", "junk"):
        if k in fn or k in fid:
            return True
    return False


def _recipient_emails_from_json(recipients) -> list[str]:
    if not recipients or not isinstance(recipients, list):
        return []
    out: list[str] = []
    for rec in recipients:
        if not isinstance(rec, dict):
            continue
        addr = rec.get("email")
        if not addr and isinstance(rec.get("emailAddress"), dict):
            addr = rec["emailAddress"].get("address")
        if addr and str(addr).strip():
            out.append(str(addr).strip().lower())
    return out


def _email_contains_project_name(email: Email, project_name: str) -> bool:
    """True only if the exact project name (case-insensitive) appears in subject or body."""
    pn = (project_name or "").strip()
    if not pn:
        return False
    pl = pn.lower()
    subj = (email.subject or "").lower()
    if pl in subj:
        return True
    preview = (email.body_preview or "").lower()
    if pl in preview:
        return True
    body = email.body_content or ""
    if len(body) > 200_000:
        body = body[:200_000]
    if pl in body.lower():
        return True
    return False


def _assert_project_mailbox_threads_access(project: TeamProject, admin_email: str, db: Session) -> None:
    """Only the creating admin may list mailbox threads (when creator is recorded)."""
    if not _is_admin_actor(db, admin_email):
        # Managers can view mailbox threads for projects in their scope (read-only pages).
        if _can_view_project_for_actor(db, admin_email, project):
            return
        raise HTTPException(status_code=403, detail="You can only access projects from your scope")
    creator_id = project.created_by_user_id
    if not creator_id:
        return
    creator = db.query(User).filter(User.id == creator_id).first()
    if not creator:
        return
    if (creator.email or "").strip().lower() != (admin_email or "").strip().lower():
        raise HTTPException(
            status_code=403,
            detail="Only the admin who created this project can view related mailbox threads.",
        )


def _can_view_project_for_actor(db: Session, actor_email: str, project: TeamProject) -> bool:
    if _is_admin_actor(db, actor_email):
        return True
    mgr = _manager_actor_row(db, actor_email)
    if not mgr:
        return False
    if mgr.team_id and project.team_id == mgr.team_id:
        return True
    scope = actor_manager_scope_mailboxes(db, actor_email) or set()
    if not scope:
        return False
    assigned = (
        db.query(User.email)
        .join(ProjectAssignment, ProjectAssignment.user_id == User.id)
        .filter(ProjectAssignment.project_id == project.id)
        .all()
    )
    for (em,) in assigned:
        if em and str(em).strip().lower() in scope:
            return True
    return False


# --- Teams ---
@router.get("/teams", response_model=list[TeamOut])
def list_teams(
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List all teams with member count."""
    teams = db.query(Team).order_by(Team.name).all()
    if not _is_admin_actor(db, actor_email):
        mgr = _manager_actor_row(db, actor_email)
        if mgr and mgr.team_id:
            teams = [t for t in teams if t.id == mgr.team_id]
        else:
            teams = []
    result = []
    for t in teams:
        count = db.query(User).filter(User.team_id == t.id).count()
        result.append(TeamOut(id=t.id, name=t.name, slug=t.slug, memberCount=count))
    return result


@router.get("/teams/{team_id}", response_model=TeamOut)
def get_team(
    team_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Get team by id with member count."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if not _is_admin_actor(db, actor_email):
        mgr = _manager_actor_row(db, actor_email)
        if not mgr or not mgr.team_id or mgr.team_id != team.id:
            raise HTTPException(status_code=403, detail="You can only access your assigned department")
    count = db.query(User).filter(User.team_id == team.id).count()
    return TeamOut(id=team.id, name=team.name, slug=team.slug, memberCount=count)


@router.get("/teams/{team_id}/status", response_model=TeamStatusOut)
def get_team_status(
    team_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Team status: counts of emails assigned, escalations, leads."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if not _is_admin_actor(db, actor_email):
        mgr = _manager_actor_row(db, actor_email)
        if not mgr or not mgr.team_id or mgr.team_id != team.id:
            raise HTTPException(status_code=403, detail="You can only access your assigned department")
    emails_assigned = db.query(Email).filter(Email.assigned_team == team.name).count() if hasattr(Email, "assigned_team") else 0
    escalations = 0
    leads = 0
    if hasattr(Email, "is_escalation"):
        escalations = db.query(Email).filter(Email.is_escalation == True, Email.assigned_team == team.name).count()
    if hasattr(Email, "lead_label"):
        leads = db.query(Email).filter(Email.lead_label.isnot(None), Email.assigned_team == team.name).count()
    return TeamStatusOut(
        teamId=team.id,
        teamName=team.name,
        emailsAssigned=emails_assigned,
        escalationsCount=escalations,
        leadsCount=leads,
    )


# --- Users ---

def _mailbox_email_counts(db: Session, mailbox_emails: list[str]) -> dict[str, int]:
    """Non-deleted email totals per mailbox (lowercase key)."""
    lowered = list({(e or "").strip().lower() for e in mailbox_emails if (e or "").strip()})
    if not lowered:
        return {}
    rows = (
        db.query(func.lower(Email.mailbox_owner_email), func.count(Email.id))
        .filter(
            Email.deleted_at.is_(None),
            func.lower(Email.mailbox_owner_email).in_(lowered),
        )
        .group_by(func.lower(Email.mailbox_owner_email))
        .all()
    )
    return {mbox: int(cnt) for mbox, cnt in rows if mbox}


def _scoped_users_query(
    db: Session,
    actor_email: str,
    *,
    role: str | None = None,
    team_id: str | None = None,
):
    """Users visible to the actor (admin: all; manager: self + reports + team)."""
    q = db.query(User).order_by(User.email)
    is_admin_actor = _is_admin_actor(db, actor_email)
    manager_row = None if is_admin_actor else _manager_actor_row(db, actor_email)
    if manager_row:
        scope_parts = [User.id == manager_row.id, User.manager_id == manager_row.id]
        if manager_row.team_id:
            scope_parts.append(User.team_id == manager_row.team_id)
        q = q.filter(or_(*scope_parts))
    if role and role.strip():
        q = q.filter(User.role == role.strip())
    if is_admin_actor and team_id and team_id.strip():
        q = q.filter(User.team_id == team_id.strip())
    return q


@router.get("/users/email-counts", response_model=list[UserEmailCountOut])
def list_user_email_counts(
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Lightweight per-user mailbox email totals for live UI refresh during ingest."""
    users = _scoped_users_query(db, actor_email).all()
    email_counts = _mailbox_email_counts(db, [u.email for u in users])
    return [
        UserEmailCountOut(
            userId=u.id,
            email=u.email,
            emailCount=email_counts.get((u.email or "").strip().lower(), 0),
        )
        for u in users
    ]


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    role: str | None = Query(None, description="Filter by role: Admin, Manager, Member"),
    team_id: str | None = Query(None, alias="teamId"),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List users with role, team, manager. Optional filter by role or team."""
    users = _scoped_users_query(db, actor_email, role=role, team_id=team_id).all()
    email_counts = _mailbox_email_counts(db, [u.email for u in users])
    result = []
    for u in users:
        team_name = u.team.name if u.team else None
        report_count = db.query(User).filter(User.manager_id == u.id).count()
        mbox_key = (u.email or "").strip().lower()
        result.append(
            UserOut(
                id=u.id,
                email=u.email,
                displayName=u.display_name,
                role=u.role,
                teamId=u.team_id,
                teamName=team_name,
                managerId=u.manager_id,
                isTeamLead=u.is_team_lead,
                reportCount=report_count,
                emailCount=email_counts.get(mbox_key, 0),
                lastLoginAt=u.last_login_at,
                createdAt=u.created_at,
            )
        )
    return result


@router.get("/retag-approvals", response_model=list[RetagApprovalOut])
def list_retag_approvals(
    db: Session = Depends(get_db),
    status: str = Query("pending"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    _auth: str = Depends(get_admin_user),
):
    q = db.query(RetagApprovalRequest).order_by(RetagApprovalRequest.requested_at.desc())
    status_v = (status or "").strip().lower()
    if status_v in ("pending", "approved", "rejected"):
        q = q.filter(RetagApprovalRequest.status == status_v)
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    out: list[RetagApprovalOut] = []
    for r in rows:
        email = db.query(Email).filter(Email.id == r.email_id).first()
        out.append(
            RetagApprovalOut(
                id=r.id,
                emailId=r.email_id,
                mailboxOwnerEmail=r.mailbox_owner_email,
                requestedByEmail=r.requested_by_email,
                requestedTeam=r.requested_team,
                status=r.status,
                requestedAt=r.requested_at,
                reviewedAt=r.reviewed_at,
                reviewedByEmail=r.reviewed_by_email,
                reviewNote=r.review_note,
                emailSubject=getattr(email, "subject", None),
                sender=getattr(email, "sender_email", None),
                receivedAt=getattr(email, "received_at", None),
            )
        )
    return out


@router.post("/retag-approvals/{request_id}/approve")
def approve_retag_request(
    request_id: str,
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_user),
):
    req = db.query(RetagApprovalRequest).filter(RetagApprovalRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Retag approval request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request is already reviewed")
    email = db.query(Email).filter(Email.id == req.email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found for this request")
    _perform_retag(db, email, req.requested_team, admin_email)
    req.status = "approved"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_email = (admin_email or "").strip().lower() or None
    db.commit()
    return {"ok": True, "requestId": request_id, "status": "approved"}


@router.post("/retag-approvals/{request_id}/reject")
def reject_retag_request(
    request_id: str,
    review_note: str | None = Query(None, alias="reviewNote"),
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_user),
):
    req = db.query(RetagApprovalRequest).filter(RetagApprovalRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Retag approval request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request is already reviewed")
    req.status = "rejected"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_email = (admin_email or "").strip().lower() or None
    req.review_note = (review_note or "").strip() or None
    db.commit()
    return {"ok": True, "requestId": request_id, "status": "rejected"}


@router.get("/recent-sign-ins", response_model=list[RecentSignInOut])
def recent_sign_ins(
    db: Session = Depends(get_db),
    limit: int = Query(30, ge=1, le=100),
    _auth: str = Depends(get_admin_user),
):
    """Recent platform access for admins (last login or account created)."""
    activity = func.coalesce(User.last_login_at, User.created_at)
    rows = db.query(User).order_by(desc(activity)).limit(limit).all()
    return [
        RecentSignInOut(
            userId=u.id,
            email=u.email,
            displayName=u.display_name,
            role=u.role,
            lastLoginAt=u.last_login_at,
            createdAt=u.created_at,
        )
        for u in rows
    ]


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    role: str | None = Query(None),
    team_id: str | None = Query(None, alias="teamId"),
    manager_id: str | None = Query(None, alias="managerId"),
    is_team_lead: bool | None = Query(None, alias="isTeamLead"),
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Update user role, team, manager, or is_team_lead."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    is_admin_actor = _is_admin_actor(db, actor_email)
    if not is_admin_actor:
        manager_row = _manager_actor_row(db, actor_email)
        if not manager_row:
            raise HTTPException(status_code=403, detail="Manager access required")
        scope_parts = [User.id == manager_row.id, User.manager_id == manager_row.id]
        if manager_row.team_id:
            scope_parts.append(User.team_id == manager_row.team_id)
        in_scope = (
            db.query(User.id)
            .filter(User.id == user_id, or_(*scope_parts))
            .first()
        )
        if not in_scope:
            raise HTTPException(status_code=403, detail="User is outside your team scope")
        if role is not None or team_id is not None or is_team_lead is not None:
            raise HTTPException(
                status_code=403,
                detail="Managers may only update reporting manager (managerId)",
            )
    old_role = user.role
    if role is not None and role.strip() in ("Admin", "Manager", "Member"):
        new_role = role.strip()
        user.role = new_role
        elevated = new_role in ("Admin", "Manager")
        was_elevated = old_role in ("Admin", "Manager")
        if elevated and (not was_elevated or new_role != old_role):
            user.role_promoted_at = datetime.now(timezone.utc)
            user.role_promotion_dismissed_at = None
        elif not elevated:
            user.role_promoted_at = None
            user.role_promotion_dismissed_at = None
    if team_id is not None:
        if team_id.strip():
            team = db.query(Team).filter(Team.id == team_id.strip()).first()
            if not team:
                raise HTTPException(status_code=400, detail="Team not found")
            user.team_id = team.id
        else:
            user.team_id = None
    if manager_id is not None:
        if manager_id.strip():
            manager = db.query(User).filter(User.id == manager_id.strip()).first()
            if not manager:
                raise HTTPException(status_code=400, detail="Manager user not found")
            mgr_role = getattr(manager, "role", None)
            target_role = getattr(user, "role", None)
            if not is_admin_actor:
                if mgr_role != "Admin":
                    raise HTTPException(
                        status_code=400,
                        detail="Reporting manager must be a user with Admin role",
                    )
            elif mgr_role == "Admin":
                pass
            elif mgr_role == "Manager" and target_role == "Member":
                if not user.team_id or manager.team_id != user.team_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Member can only report to a manager on the same team",
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid reporting manager for this user's role",
                )
            user.manager_id = manager.id
        else:
            user.manager_id = None
    if is_team_lead is not None:
        user.is_team_lead = is_team_lead
    db.commit()
    return {"ok": True, "userId": user_id}


@router.post("/users")
def create_user(
    email: str = Query(...),
    display_name: str | None = Query(None, alias="displayName"),
    role: str = Query("Member"),
    team_id: str | None = Query(None, alias="teamId"),
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_user),
):
    """Create a user (for sync or manual add)."""
    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")
    if role not in ("Admin", "Manager", "Member"):
        role = "Member"
    now = datetime.now(timezone.utc)
    user = User(email=email, display_name=display_name or email.split("@")[0], role=role)
    if role in ("Admin", "Manager"):
        user.role_promoted_at = now
        user.role_promotion_dismissed_at = None
    if team_id and team_id.strip():
        team = db.query(Team).filter(Team.id == team_id.strip()).first()
        if team:
            user.team_id = team.id
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "userId": user.id, "email": user.email}


# --- Projects workflow (admin only) ---
@router.get("/projects-workflow", response_model=list[TeamProjectOut])
def list_projects_workflow(
    db: Session = Depends(get_db),
    team_id: str | None = Query(None, alias="teamId"),
    status: str | None = Query(None),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    try:
        q = db.query(TeamProject).order_by(TeamProject.updated_at.desc())
        if _is_admin_actor(db, actor_email) and team_id and team_id.strip():
            q = q.filter(TeamProject.team_id == team_id.strip())
        if status and status.strip():
            q = q.filter(TeamProject.status == status.strip().lower())
        projects = q.all()
        if not _is_admin_actor(db, actor_email):
            projects = [p for p in projects if _can_view_project_for_actor(db, actor_email, p)]
        return [_serialize_project(db, p) for p in projects]
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


@router.get("/projects-workflow/{project_id}", response_model=TeamProjectOut)
def get_project_workflow(
    project_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Single project for admin workflow detail view."""
    try:
        project = db.query(TeamProject).filter(TeamProject.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if not _can_view_project_for_actor(db, actor_email, project):
            raise HTTPException(status_code=403, detail="You can only access projects from your scope")
        return _serialize_project(db, project)
    except HTTPException:
        raise
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


@router.post("/projects-workflow", response_model=TeamProjectOut)
def create_project_workflow(
    payload: TeamProjectUpsertIn,
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_user),
):
    try:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name is required")
        status = (payload.status or "running").strip().lower()
        if status not in ("running", "new", "planned", "completed"):
            status = "running"
        team_id = (payload.teamId or "").strip() or None
        if team_id:
            team = db.query(Team).filter(Team.id == team_id).first()
            if not team:
                raise HTTPException(status_code=400, detail="Team not found")

        items = _effective_project_assignments(payload)
        valid = _assignment_valid_user_ids(items, db)
        lead_for_project = _resolve_project_lead_for_save(payload.projectLeadUserId, valid)
        creator_id = _user_id_for_admin_email(db, admin_email)

        project = TeamProject(
            name=name,
            team_id=team_id,
            status=status,
            structure=payload.structure if isinstance(payload.structure, dict) else None,
            project_lead_user_id=lead_for_project,
            created_by_user_id=creator_id,
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        if items:
            for item in items:
                uid = (item.userId or "").strip()
                if uid not in valid:
                    continue
                role = (item.role or "").strip()[:64] or None
                resp = (item.responsibilities or "").strip() or None
                reports_to = _normalize_project_reports_to(uid, item.reportsToUserId, valid)
                db.add(
                    ProjectAssignment(
                        project_id=project.id,
                        user_id=uid,
                        role=role,
                        responsibilities=resp,
                        reports_to_user_id=reports_to,
                    )
                )
            db.commit()
            db.refresh(project)

        return _serialize_project(db, project)
    except HTTPException:
        raise
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


@router.patch("/projects-workflow/{project_id}", response_model=TeamProjectOut)
def update_project_workflow(
    project_id: str,
    payload: TeamProjectUpsertIn,
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_user),
):
    try:
        project = db.query(TeamProject).filter(TeamProject.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name is required")
        status = (payload.status or "running").strip().lower()
        if status not in ("running", "new", "planned", "completed"):
            status = "running"
        team_id = (payload.teamId or "").strip() or None
        if team_id:
            team = db.query(Team).filter(Team.id == team_id).first()
            if not team:
                raise HTTPException(status_code=400, detail="Team not found")

        project.name = name
        project.status = status
        project.team_id = team_id
        project.structure = payload.structure if isinstance(payload.structure, dict) else None
        if not project.created_by_user_id:
            project.created_by_user_id = _user_id_for_admin_email(db, admin_email)

        db.query(ProjectAssignment).filter(ProjectAssignment.project_id == project.id).delete()
        items = _effective_project_assignments(payload)
        valid = _assignment_valid_user_ids(items, db)
        project.project_lead_user_id = _resolve_project_lead_for_save(payload.projectLeadUserId, valid)

        if items:
            for item in items:
                uid = (item.userId or "").strip()
                if uid not in valid:
                    continue
                role = (item.role or "").strip()[:64] or None
                resp = (item.responsibilities or "").strip() or None
                reports_to = _normalize_project_reports_to(uid, item.reportsToUserId, valid)
                db.add(
                    ProjectAssignment(
                        project_id=project.id,
                        user_id=uid,
                        role=role,
                        responsibilities=resp,
                        reports_to_user_id=reports_to,
                    )
                )
        db.commit()
        db.refresh(project)
        return _serialize_project(db, project)
    except HTTPException:
        raise
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


@router.get(
    "/projects-workflow/{project_id}/mailbox-threads",
    response_model=ConversationsResponse,
    response_model_by_alias=True,
)
def list_project_mailbox_threads(
    project_id: str,
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_or_manager_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
):
    """
    Threads in the signed-in user's mailbox where at least one Inbox/Spam/Junk message contains the project name.
    Counts and previews use only messages that contain the project name (not the whole reply chain).
    """
    try:
        project = db.query(TeamProject).filter(TeamProject.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        _assert_project_mailbox_threads_access(project, admin_email, db)

        mailbox = (admin_email or "").strip().lower()

        candidates = (
            db.query(Email)
            .filter(
                Email.mailbox_owner_email == mailbox,
                Email.conversation_id.isnot(None),
                Email.conversation_id != "",
            )
            .order_by(Email.received_at.desc())
            .limit(8000)
            .all()
        )
        seed_emails = [
            e
            for e in candidates
            if _folder_is_inbox_spam_junk(e) and not (e.conversation_id or "").strip().startswith("thread:")
        ]
        matched_seeds = [e for e in seed_emails if _email_contains_project_name(e, project.name)]
        cids = {(e.conversation_id or "").strip() for e in matched_seeds if e.conversation_id}

        if not cids:
            return ConversationsResponse(conversations=[], total=0, page=page, pageSize=page_size)

        cid_list = list(cids)
        all_in_threads = (
            db.query(Email)
            .filter(
                Email.mailbox_owner_email == mailbox,
                Email.conversation_id.in_(cid_list),
            )
            .order_by(Email.received_at.desc())
            .all()
        )
        by_cid: dict[str, list] = {}
        for r in all_in_threads:
            cid = (r.conversation_id or "").strip()
            if not cid or cid.startswith("thread:"):
                continue
            by_cid.setdefault(cid, []).append(r)

        threads: list[ConversationOut] = []
        for cid, emails in by_cid.items():
            matching = [e for e in emails if _email_contains_project_name(e, project.name)]
            if not matching:
                continue
            matching_sorted = sorted(matching, key=lambda x: x.received_at, reverse=True)
            latest = matching_sorted[0]
            participants: set[str] = set()
            for e in matching_sorted[:20]:
                if e.sender_email:
                    participants.add(e.sender_email.strip().lower())
                for addr in _recipient_emails_from_json(e.to_recipients):
                    participants.add(addr)
                for addr in _recipient_emails_from_json(e.cc_recipients):
                    participants.add(addr)
            participants.discard("")
            preview = ", ".join(sorted(participants)[:5])
            if len(participants) > 5:
                preview += "…"
            threads.append(
                ConversationOut(
                    conversationId=cid,
                    subject=latest.subject,
                    lastReceivedAt=latest.received_at,
                    messageCount=len(matching_sorted),
                    participantsPreview=preview,
                )
            )
        threads.sort(key=lambda t: t.last_received_at, reverse=True)
        total = len(threads)
        start = (page - 1) * page_size
        page_threads = threads[start : start + page_size]
        return ConversationsResponse(conversations=page_threads, total=total, page=page, pageSize=page_size)
    except HTTPException:
        raise
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


@router.get(
    "/projects-workflow/{project_id}/mailbox-threads/{conversation_id:path}/emails",
    response_model=ThreadEmailsResponse,
    response_model_by_alias=True,
)
def get_project_mailbox_conversation_emails(
    project_id: str,
    conversation_id: str,
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_or_manager_user),
):
    """
    Messages in this conversation that contain the project name only (chronological).
    Same mailbox and access rules as list_project_mailbox_threads.
    """
    try:
        project = db.query(TeamProject).filter(TeamProject.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        _assert_project_mailbox_threads_access(project, admin_email, db)

        mailbox = (admin_email or "").strip().lower()
        rows = (
            db.query(Email)
            .filter(
                Email.mailbox_owner_email == mailbox,
                Email.conversation_id == conversation_id,
            )
            .order_by(Email.received_at.asc())
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Conversation not found")
        filtered = [e for e in rows if _email_contains_project_name(e, project.name)]
        if not filtered:
            raise HTTPException(
                status_code=404,
                detail="No messages in this thread contain the project name.",
            )
        out: list[EmailDetailOut] = []
        for email in filtered:
            atts = db.query(Attachment).filter(Attachment.email_id == email.id).all()
            out.append(
                EmailDetailOut(
                    id=email.id,
                    messageId=email.message_id,
                    subject=email.subject,
                    sender=email.sender_email,
                    senderDisplayName=email.sender_display_name,
                    toRecipients=email.to_recipients or [],
                    ccRecipients=email.cc_recipients or [],
                    bccRecipients=_bcc_from_email(email),
                    receivedAt=email.received_at,
                    sentAt=email.sent_at,
                    folder=email.folder_name or email.folder_id or "",
                    bodyPreview=email.body_preview,
                    bodyContent=email.body_content,
                    bodyContentType=email.body_content_type,
                    attachments=[
                        AttachmentOut(
                            id=a.id,
                            name=a.name,
                            content_type=a.content_type,
                            size=a.size,
                            is_inline=a.is_inline,
                        )
                        for a in atts
                    ],
                    status=email.status,
                    summary=getattr(email, "ai_summary", None) or None,
                    category=_display_category(email),
                    priorityLabel=getattr(email, "ai_priority_label", None),
                    priorityScore=getattr(email, "ai_priority_score", None),
                    suggestedReplies=getattr(email, "ai_suggested_replies", None) or [],
                    aiStatus=getattr(email, "ai_status", None),
                    aiProcessedAt=getattr(email, "ai_processed_at", None),
                    processingStatus=getattr(email, "processing_status", None),
                    aiErrorMessage=getattr(email, "ai_error_message", None),
                    graphId=getattr(email, "graph_id", None),
                )
            )
        return ThreadEmailsResponse(conversationId=conversation_id, emails=out)
    except HTTPException:
        raise
    except (OperationalError, ProgrammingError) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Projects tables are not in the database yet. From the backend folder run: "
                "alembic upgrade head"
            ),
        ) from e


# --- Escalations by user (admin: list users with counts, then view one user's escalations) ---
@router.get("/escalation-counts", response_model=list[UserEscalationCountOut])
def list_escalation_counts_by_user(
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List all users with their escalation email count and status (read/unread/replied). Excludes default/system mailbox."""
    excluded = _excluded_mailboxes_for_user_lists()
    users = db.query(User).order_by(User.email).all()
    users = [u for u in users if (u.email or "").strip().lower() not in excluded]
    scope = actor_manager_scope_mailboxes(db, actor_email)
    if scope is not None:
        users = [u for u in users if (u.email or "").strip().lower() in scope]
    if not hasattr(Email, "is_escalation") or not hasattr(Email, "mailbox_owner_email"):
        return [
            UserEscalationCountOut(
                email=u.email, displayName=u.display_name, escalationCount=0,
                readCount=0, unreadCount=0, repliedCount=0,
            )
            for u in users
        ]
    E2 = aliased(Email)
    result = []
    for u in users:
        base = db.query(Email).filter(
            Email.is_escalation == True,
            Email.mailbox_owner_email == u.email,
        )
        count = base.count()
        read_count = base.filter(Email.is_read == True).count() if hasattr(Email, "is_read") else 0
        unread_count = base.filter(Email.is_read == False).count() if hasattr(Email, "is_read") else 0
        # Replied: distinct escalation conversations where the user has at least one sent message in that thread
        if hasattr(Email, "conversation_id") and hasattr(Email, "folder_id") and hasattr(Email, "folder_name"):
            sent_match = or_(
                E2.folder_id == "sentitems",
                func.coalesce(func.lower(E2.folder_name), "").like("%sent%"),
            )
            replied_exists = exists().where(
                E2.conversation_id == Email.conversation_id,
                E2.mailbox_owner_email == u.email,
                sent_match,
            )
            replied_count = (
                db.query(func.count(func.distinct(Email.conversation_id)))
                .filter(
                    Email.is_escalation == True,
                    Email.mailbox_owner_email == u.email,
                    Email.conversation_id.isnot(None),
                    replied_exists,
                )
                .scalar()
                or 0
            )
        else:
            replied_count = 0
        result.append(UserEscalationCountOut(
            email=u.email,
            displayName=u.display_name,
            escalationCount=count,
            readCount=read_count,
            unreadCount=unread_count,
            repliedCount=replied_count,
        ))
    return result


@router.get("/escalations")
def list_escalations_for_user(
    mailbox: str = Query(..., description="User email (mailbox) to show escalations for"),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    team: str | None = Query(None, description="Filter by assigned team"),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List escalation emails for a specific user's mailbox. Admin or Manager."""
    mailbox = (mailbox or "").strip().lower()
    if not mailbox or "@" not in mailbox:
        raise HTTPException(status_code=400, detail="Valid mailbox (user email) required")
    assert_mailbox_in_manager_scope(db, actor_email, mailbox)
    try:
        if not hasattr(Email, "is_escalation"):
            return {"escalations": [], "total": 0, "page": page, "pageSize": page_size}
        q = db.query(Email).filter(Email.is_escalation == True, Email.mailbox_owner_email == mailbox)
        q = apply_received_at_date_range_filter(q, Email.received_at, from_date, to_date)
        if team and team.strip() and hasattr(Email, "assigned_team"):
            q = q.filter(Email.assigned_team == team.strip())
        total = q.count()
        rows = q.order_by(Email.received_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {
            "escalations": [_email_to_item(r, include_escalation_reasons=True) for r in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    except (OperationalError, Exception):
        return {"escalations": [], "total": 0, "page": page, "pageSize": page_size}


# --- Leads by user (admin: list users with counts, then view one user's leads) ---
@router.get("/lead-counts", response_model=list[UserLeadCountOut])
def list_lead_counts_by_user(
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List all users with their lead email count and status (read/unread/replied). Excludes default/system mailbox."""
    excluded = _excluded_mailboxes_for_user_lists()
    users = db.query(User).order_by(User.email).all()
    users = [u for u in users if (u.email or "").strip().lower() not in excluded]
    scope = actor_manager_scope_mailboxes(db, actor_email)
    if scope is not None:
        users = [u for u in users if (u.email or "").strip().lower() in scope]
    if not hasattr(Email, "lead_label") or not hasattr(Email, "mailbox_owner_email"):
        return [
            UserLeadCountOut(
                email=u.email, displayName=u.display_name, leadCount=0,
                readCount=0, unreadCount=0, repliedCount=0,
            )
            for u in users
        ]
    E2 = aliased(Email)
    result = []
    for u in users:
        base = db.query(Email).filter(
            Email.lead_label.isnot(None),
            Email.mailbox_owner_email == u.email,
        )
        count = base.count()
        read_count = base.filter(Email.is_read == True).count() if hasattr(Email, "is_read") else 0
        unread_count = base.filter(Email.is_read == False).count() if hasattr(Email, "is_read") else 0
        if hasattr(Email, "conversation_id") and hasattr(Email, "folder_id") and hasattr(Email, "folder_name"):
            sent_match = or_(
                E2.folder_id == "sentitems",
                func.coalesce(func.lower(E2.folder_name), "").like("%sent%"),
            )
            replied_exists = exists().where(
                E2.conversation_id == Email.conversation_id,
                E2.mailbox_owner_email == u.email,
                sent_match,
            )
            replied_count = (
                db.query(func.count(func.distinct(Email.conversation_id)))
                .filter(
                    Email.lead_label.isnot(None),
                    Email.mailbox_owner_email == u.email,
                    Email.conversation_id.isnot(None),
                    replied_exists,
                )
                .scalar()
                or 0
            )
        else:
            replied_count = 0
        result.append(UserLeadCountOut(
            email=u.email,
            displayName=u.display_name,
            leadCount=count,
            readCount=read_count,
            unreadCount=unread_count,
            repliedCount=replied_count,
        ))
    return result


@router.get("/leads")
def list_leads_for_user(
    mailbox: str = Query(..., description="User email (mailbox) to show leads for"),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    label: str | None = Query(None, description="Filter by lead label: Hot, Warm, Cold"),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    team: str | None = Query(None, description="Filter by assigned team"),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """List lead emails for a specific user's mailbox. Admin or Manager."""
    mailbox = (mailbox or "").strip().lower()
    if not mailbox or "@" not in mailbox:
        raise HTTPException(status_code=400, detail="Valid mailbox (user email) required")
    assert_mailbox_in_manager_scope(db, actor_email, mailbox)
    try:
        if not hasattr(Email, "lead_label"):
            return {"leads": [], "total": 0, "page": page, "pageSize": page_size}
        q = db.query(Email).filter(Email.lead_label.isnot(None), Email.mailbox_owner_email == mailbox)
        if label and label.strip():
            q = q.filter(Email.lead_label == label.strip())
        q = apply_received_at_date_range_filter(q, Email.received_at, from_date, to_date)
        if team and team.strip() and hasattr(Email, "assigned_team"):
            q = q.filter(Email.assigned_team == team.strip())
        total = q.count()
        rows = q.order_by(Email.received_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {
            "leads": [_email_to_item(r, include_lead_label=True) for r in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    except (OperationalError, Exception):
        return {"leads": [], "total": 0, "page": page, "pageSize": page_size}


@router.get("/retagged")
def admin_list_retagged_for_mailbox(
    mailbox: str = Query(..., description="Mailbox owner email"),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500, alias="pageSize"),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    _auth: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Retagged emails (ex escalation/lead) for a user's mailbox. Admin or Manager."""
    mailbox_l = (mailbox or "").strip().lower()
    if not mailbox_l or "@" not in mailbox_l:
        raise HTTPException(status_code=400, detail="Valid mailbox (user email) required")
    assert_mailbox_in_manager_scope(db, actor_email, mailbox_l)
    try:
        if not hasattr(Email, "retagged_at"):
            return {"retagged": [], "total": 0, "page": page, "pageSize": page_size}
        q = db.query(Email).filter(
            Email.retagged_at.isnot(None),
            Email.mailbox_owner_email == mailbox_l,
        )
        q = apply_received_at_date_range_filter(q, Email.received_at, from_date, to_date)
        total = q.count()
        rows = q.order_by(Email.retagged_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {
            "retagged": [_email_to_item(r) for r in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    except (OperationalError, Exception):
        return {"retagged": [], "total": 0, "page": page, "pageSize": page_size}


class SyncOutlookDeletedBody(BaseModel):
    """Optional lookback window per mailbox; default uses OUTLOOK_DELETED_SYNC_DAYS from settings."""

    days: int | None = Field(None, ge=1, le=90)


@router.post("/sync-outlook-deleted")
def admin_sync_outlook_deleted(
    body: SyncOutlookDeletedBody | None = Body(None),
    _admin: str = Depends(get_admin_user),
):
    """
    Enqueue Graph sync of the Deleted Items folder for every distinct `users.email` (registered sign-ins).
    Requires Celery worker + app-only Mail.Read for those mailboxes. Complements in-app “Remove from History”.
    """
    from app.workers.tasks import sync_outlook_deleted_for_all_users_task

    d = body.days if body else None
    task = sync_outlook_deleted_for_all_users_task.delay(d)
    return {
        "ok": True,
        "taskId": task.id,
        "message": "Outlook Deleted Items sync enqueued for all registered user mailboxes. Run a Celery worker to process jobs.",
    }


@router.post("/sync-mailbox-message-rules")
def admin_sync_mailbox_message_rules(_admin: str = Depends(get_admin_user)):
    """
    Enqueue inbox messageRules sync for every registered `users.email`.
    Requires Celery + Graph application permission MailboxSettings.Read (admin consent).
    Runs even when OUTLOOK_MESSAGE_RULES_SYNC_ENABLED is false (manual override for Beat).
    """
    from app.workers.tasks import sync_message_rules_for_all_users_task

    task = sync_message_rules_for_all_users_task.delay(True)
    return {
        "ok": True,
        "taskId": task.id,
        "message": "Mailbox message rules sync enqueued for all registered user mailboxes. Run a Celery worker.",
    }


@router.get("/emails", response_model=EmailsResponse, response_model_by_alias=True)
def admin_list_emails(
    db: Session = Depends(get_db),
    _admin: str = Depends(get_admin_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500, alias="pageSize"),
    search: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    category: str | None = Query(None, description="Filter by AI category"),
    deleted_only: bool = Query(False, alias="deletedOnly"),
    external_participants: bool = Query(
        False,
        alias="externalParticipants",
        description="If true, only messages with sender or any to/cc/bcc address outside company_internal_email_domain",
    ),
    external_senders_only: bool = Query(
        False,
        alias="externalSendersOnly",
        description="If true, only messages whose From address is outside company_internal_email_domain (e.g. not @cachedigitech.com).",
    ),
    mail_direction: str | None = Query(
        None,
        alias="mailDirection",
        description="Optional sent|received based on synced folder (Sent vs non-Sent, e.g. Inbox).",
    ),
):
    """
    Admin-only: list emails across all synced mailboxes.
    deletedOnly=false: active mail (not soft-deleted).
    deletedOnly=true: in-app removed History and/or messages found in Outlook Deleted Items (after sync).
    externalParticipants=true: cross-domain traffic vs settings.company_internal_email_domain (same rows as mailbox; filtered view).
    externalSendersOnly=true: From address not on company internal domain (typical “inbox from outside”).
    mailDirection=sent|received: restrict to Sent Items vs other folders (uses folder_name from Graph sync).
    """
    try:
        q = db.query(Email)
        if deleted_only:
            q = q.filter(Email.deleted_at.isnot(None))
        else:
            q = q.filter(Email.deleted_at.is_(None))
        if external_senders_only:
            dom = (get_settings().company_internal_email_domain or "cachedigitech.com").strip().lower().lstrip("@")
            if dom:
                q = q.filter(
                    Email.sender_email.isnot(None),
                    func.trim(Email.sender_email) != "",
                    not_(func.lower(Email.sender_email).like(f"%@{dom}")),
                )
        if external_participants:
            dom = getattr(get_settings(), "company_internal_email_domain", None) or "cachedigitech.com"
            ext = _external_participant_sql_filter(dom)
            if ext is not None:
                q = q.filter(ext)
        md = (mail_direction or "").strip().lower()
        if md == "sent":
            q = q.filter(_admin_sent_folder_clause())
        elif md == "received":
            q = q.filter(not_(_admin_sent_folder_clause()))
        if search and search.strip():
            s = f"%{search.strip()}%"
            q = q.filter(
                (Email.subject.ilike(s))
                | (Email.sender_email.ilike(s))
                | (Email.message_id.ilike(s))
                | (Email.mailbox_owner_email.ilike(s))
            )
        if category and category.strip():
            q = q.filter(Email.ai_category == category.strip())
        if from_date:
            try:
                dt = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
                q = q.filter(Email.received_at >= dt)
            except ValueError:
                pass
        if to_date:
            try:
                dt = datetime.fromisoformat(to_date.replace("Z", "+00:00"))
                dt = dt + timedelta(days=1)
                q = q.filter(Email.received_at < dt)
            except ValueError:
                pass
        total = q.count()
        rows = q.order_by(Email.received_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
        emails = [
            EmailOut(
                id=r.id,
                messageId=r.message_id,
                subject=r.subject,
                sender=r.sender_email,
                receivedAt=r.received_at,
                folder=r.folder_name or r.folder_id or "",
                status=r.status,
                summary=getattr(r, "ai_summary", None) or None,
                category=_display_category(r),
                priorityLabel=getattr(r, "ai_priority_label", None),
                priorityScore=getattr(r, "ai_priority_score", None),
                aiStatus=getattr(r, "ai_status", None),
                aiProcessedAt=getattr(r, "ai_processed_at", None),
                processingStatus=getattr(r, "processing_status", None),
                assignedTeam=getattr(r, "assigned_team", None),
                mailboxOwnerEmail=getattr(r, "mailbox_owner_email", None),
                deletedAt=getattr(r, "deleted_at", None) if deleted_only else None,
            )
            for r in rows
        ]
        return EmailsResponse(emails=emails, total=total, page=page, pageSize=page_size)
    except (OperationalError, Exception):
        return EmailsResponse(emails=[], total=0, page=page, pageSize=page_size)


@router.post("/emails/{email_id}/restore")
def admin_restore_email(
    email_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """Clear soft-delete so the message appears again in the owner's History."""
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    email.deleted_at = None
    email.deleted_by_email = None
    db.commit()
    return {"ok": True, "emailId": email_id}


@router.patch("/emails/{email_id}/retag")
def admin_retag_email(
    email_id: str,
    mailbox: str = Query(..., description="Mailbox owner email (must match the email row)"),
    body: RetagEmailBody = Body(...),
    db: Session = Depends(get_db),
    admin_email: str = Depends(get_admin_or_manager_user),
    actor_email: str = Depends(get_current_user_email),
):
    """Retag mail in another user's mailbox (immediate apply). Admin or Manager."""
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    owner = (getattr(email, "mailbox_owner_email", None) or "").strip().lower()
    if owner != (mailbox or "").strip().lower():
        raise HTTPException(status_code=400, detail="Email does not belong to the specified mailbox")
    assert_mailbox_in_manager_scope(db, actor_email, owner)
    _perform_retag(db, email, body.assigned_team, admin_email)
    return {"ok": True, "emailId": email_id, "assignedTeam": email.assigned_team}


# --- Workflow (who leads whom) ---
@router.get("/workflow", response_model=list[WorkflowNode])
def get_workflow(
    db: Session = Depends(get_db),
    _auth: str = Depends(get_admin_user),
):
    """Return flat list of users with reportIds for building tree (Manager -> members)."""
    users = db.query(User).order_by(User.email).all()
    report_map: dict[str, list[str]] = {}
    for u in users:
        if u.manager_id:
            report_map.setdefault(u.manager_id, []).append(u.id)

    projects_by_user: dict[str, list[str]] = {}
    for user_id, proj_name in (
        db.query(ProjectAssignment.user_id, TeamProject.name)
        .join(TeamProject, ProjectAssignment.project_id == TeamProject.id)
        .all()
    ):
        projects_by_user.setdefault(user_id, []).append(proj_name)
    for uid in projects_by_user:
        projects_by_user[uid] = sorted(set(projects_by_user[uid]))

    result = []
    for u in users:
        result.append(
            WorkflowNode(
                id=u.id,
                email=u.email,
                displayName=u.display_name,
                role=u.role,
                teamName=u.team.name if u.team else None,
                isTeamLead=u.is_team_lead,
                managerId=u.manager_id,
                reportIds=report_map.get(u.id, []),
                projectNames=projects_by_user.get(u.id, []),
            )
        )
    return result
