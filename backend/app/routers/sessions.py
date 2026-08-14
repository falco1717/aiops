from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Event, Run, Session, User
from ..schemas import (
    CapabilityOut,
    EventOut,
    PromptIn,
    RunOut,
    SessionIn,
    SessionOut,
    SessionPatch,
    TranscriptOut,
)
from ..runner import runner
from ..security import current_user
from ..services import (
    APPROVAL_MODES,
    ValidationError,
    build_session,
    queue_run,
    validate_session_targets,
)
from ..skills import discover

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _get(db: AsyncSession, session_id: str) -> Session:
    sess = await db.get(Session, session_id)
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return sess


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    archived: bool = False,
    limit: int = Query(100, le=500),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(Session)
        .where(Session.archived == archived)
        .order_by(desc(Session.updated_at))
        .limit(limit)
    )
    return list(rows)


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        sess = await build_session(
            db,
            provider=payload.provider,
            title=payload.title,
            model=payload.model,
            preset_id=payload.preset_id,
            workspace_id=payload.workspace_id,
            account_id=payload.account_id,
            approval_mode=payload.approval_mode,
            user=user,
        )
        await db.commit()
        await db.refresh(sess)
        if payload.prompt:
            await queue_run(db, sess, payload.prompt)
            await db.refresh(sess)
    except ValidationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return sess


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    return await _get(db, session_id)


@router.patch("/{session_id}", response_model=SessionOut)
async def patch_session(
    session_id: str,
    payload: SessionPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await _get(db, session_id)
    data = payload.model_dump(exclude_unset=True)
    # Re-point fields go through the same validation as creation. Without this
    # a user could create a session on an account they may use and then patch
    # it onto a restricted one, and a bad id would surface as a 500 from the
    # foreign key rather than a 400.
    try:
        await validate_session_targets(
            db,
            provider=sess.provider,
            account_id=data.get("account_id", sess.account_id),
            preset_id=data.get("preset_id", sess.preset_id),
            workspace_id=data.get("workspace_id", sess.workspace_id),
            user=user,
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    mode = data.get("approval_mode")
    if mode is not None and mode not in APPROVAL_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"approval_mode must be one of {', '.join(APPROVAL_MODES)} (got {mode!r})",
        )

    for key, value in data.items():
        setattr(sess, key, value)
    await db.commit()
    await db.refresh(sess)
    return sess


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    sess = await _get(db, session_id)
    # Stop the agent first. Deleting the row out from under a running process
    # leaves it writing events against a session that no longer exists, which
    # fails on the foreign key and orphans the subprocess.
    active = list(
        await db.scalars(
            select(Run.id).where(
                Run.session_id == session_id, Run.status.in_(("queued", "running"))
            )
        )
    )
    for run_id in active:
        await runner.cancel(run_id)
    await db.delete(sess)
    await db.commit()


@router.get("/{session_id}/transcript", response_model=TranscriptOut)
async def transcript(
    session_id: str,
    since_event_id: int = 0,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full conversation history. `since_event_id` lets a reconnecting client
    fetch only what it missed while the websocket was down."""
    sess = await _get(db, session_id)
    runs = await db.scalars(
        select(Run).where(Run.session_id == session_id).order_by(Run.id)
    )
    events = await db.scalars(
        select(Event)
        .where(Event.session_id == session_id, Event.id > since_event_id)
        .order_by(Event.id)
    )
    return TranscriptOut(session=sess, runs=list(runs), events=list(events))


@router.post("/{session_id}/prompt", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def send_prompt(
    session_id: str,
    payload: PromptIn,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await _get(db, session_id)
    busy = await db.scalar(
        select(Run.id)
        .where(Run.session_id == session_id, Run.status.in_(("queued", "running")))
        .limit(1)
    )
    if busy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This session is still working on the previous turn. Cancel it or wait.",
        )
    try:
        return await queue_run(db, sess, payload.prompt)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/{session_id}/runs", response_model=list[RunOut])
async def list_runs(
    session_id: str, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await _get(db, session_id)
    rows = await db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.id))
    return list(rows)


@router.get("/{session_id}/events", response_model=list[EventOut])
async def list_events(
    session_id: str,
    run_id: int | None = None,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get(db, session_id)
    stmt = select(Event).where(Event.session_id == session_id)
    if run_id is not None:
        stmt = stmt.where(Event.run_id == run_id)
    rows = await db.scalars(stmt.order_by(Event.id))
    return list(rows)


@router.get("/{session_id}/capabilities", response_model=list[CapabilityOut])
async def capabilities(
    session_id: str, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Skills and slash commands this session can use.

    These already work by typing `/name` into the prompt; this endpoint just
    lets the composer show what exists rather than relying on memory.
    """
    sess = await _get(db, session_id)
    workspace_path = sess.workspace.path if sess.workspace else None
    return [
        CapabilityOut(**vars(cap))
        for cap in discover(
            sess.provider,
            workspace_path,
            sess.available_commands,
            sess.account.config_dir if sess.account else None,
        )
    ]


@router.get("/{session_id}/events/{event_id}/raw")
async def event_raw(
    session_id: str,
    event_id: int,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if event is None or event.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return {"raw": event.raw}
