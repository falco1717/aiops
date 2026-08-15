from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import can_see_session, sessions_visible_to
from ..approvals import broker, run_tokens
from ..config import settings
from ..db import get_db
from ..models import Approval, Session, User
from ..names import display_name
from ..schemas import ApprovalDecision, ApprovalOut
from ..security import current_user

log = logging.getLogger("aiops.approvals")

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# The bridge that agents call back on. Separate from the user-facing router
# because it authenticates with a per-run token rather than a session cookie.
internal = APIRouter(prefix="/api/internal", tags=["internal"], include_in_schema=False)


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    session_id: str | None = None,
    run_id: int | None = None,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Only approvals belonging to sessions this user can see.

    Answering one runs a command on this server on the agent's behalf, so the
    list has to follow session visibility exactly: anything shown here is an
    action the viewer is entitled to authorise.
    """
    stmt = select(Approval).where(
        Approval.session_id.in_(select(Session.id).where(sessions_visible_to(user)))
    )
    if session_id:
        stmt = stmt.where(Approval.session_id == session_id)
    if run_id is not None:
        stmt = stmt.where(Approval.run_id == run_id)
    if status_filter:
        stmt = stmt.where(Approval.status == status_filter)
    rows = await db.scalars(stmt.order_by(desc(Approval.id)).limit(limit))
    return list(rows)


@router.post("/{approval_id}/decide", response_model=ApprovalOut)
async def decide(
    approval_id: int,
    payload: ApprovalDecision,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Approval, approval_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    # Checked before the status below, so an approval on a session this user
    # cannot see gives nothing away — not even that it has already been answered.
    sess = await db.get(Session, row.session_id)
    if sess is None or not await can_see_session(db, sess, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    if row.status != "pending":
        # Two people can be watching the same run. Losing the race is normal,
        # so say what happened — and who did it, now that "somebody else" is a
        # real person with a name rather than always being you.
        decider = await db.get(User, row.decided_by_id) if row.decided_by_id else None
        by = f" by {display_name(decider)}" if decider else ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Already {row.status}{by}"
            + (f" — {row.note}" if row.note else "")
            + ". The agent is no longer waiting on this.",
        )
    ok = await broker.decide(
        approval_id, allowed=payload.allowed, note=payload.note, user_id=user.id
    )
    if not ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Nothing is waiting on this any more — the run ended or timed out.",
        )
    await db.refresh(row)
    return row


@internal.post("/approvals")
async def bridge_request(request: Request):
    """Called by an agent's approval bridge; blocks until a human answers.

    The response shape is deliberately provider-neutral — each bridge
    translates it into whatever its CLI expects.
    """
    payload = await request.json()
    token = payload.get("token") or request.headers.get("x-aiops-token") or ""
    resolved = run_tokens.resolve(token)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or expired run token")
    run_id, session_id = resolved

    try:
        decision = await broker.request(
            run_id=run_id,
            session_id=session_id,
            provider=str(payload.get("provider") or "claude"),
            kind=str(payload.get("kind") or "tool"),
            tool_name=payload.get("tool_name"),
            summary=payload.get("summary"),
            request=payload.get("input") if isinstance(payload.get("input"), dict) else None,
            timeout=settings.approval_timeout_seconds,
        )
    except asyncio.CancelledError:
        # The run went away underneath us; let the agent fail closed.
        return {"allowed": False, "note": "AIOps stopped waiting."}

    return {
        "allowed": decision.allowed,
        "note": decision.note,
        "updated_input": decision.updated_input,
    }
