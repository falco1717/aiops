from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import attachments as store
from ..config import settings
from ..db import get_db
from ..models import Attachment, Event, Run, Session, User
from ..schemas import (
    AttachmentOut,
    CapabilityOut,
    EventOut,
    PromptIn,
    RunOut,
    SessionFile,
    SessionFilesOut,
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

#: Downloads are user-controlled bytes on the same origin as the session cookie.
#: The Content-Type is already restricted to types a browser will not execute
#: (see attachments.download_type); this stops it guessing a different one.
_DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff"}


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
            await queue_run(db, sess, payload.prompt, attachment_ids=payload.attachment_ids)
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
    store.discard_session(session_id)


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
    # Always the full set, never the incremental slice: these hang off runs
    # rather than events, and the client redraws every message from this.
    files = await db.scalars(
        select(Attachment)
        .where(Attachment.session_id == session_id)
        .order_by(Attachment.created_at)
    )
    return TranscriptOut(
        session=sess,
        runs=list(runs),
        events=list(events),
        attachments=list(files),
    )


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
        return await queue_run(db, sess, payload.prompt, attachment_ids=payload.attachment_ids)
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


# --- attachments: files the operator hands to the agent -------------------
@router.post(
    "/{session_id}/attachments",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Take one file and park it until a prompt claims it.

    The client's filename decides nothing about where this lands: the row's
    generated id is the directory, so two uploads of screenshot.png cannot
    collide, and a name of `../../etc/passwd` is just a name.
    """
    await _get(db, session_id)
    filename = store.safe_filename(file.filename)
    attachment_id = str(uuid.uuid4())
    try:
        size = await store.save_upload(session_id, attachment_id, filename, file)
    except store.AttachmentTooLarge as exc:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Attachments are limited to {store.human_size(settings.max_attachment_bytes)}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not store the upload: {exc}"
        ) from exc

    row = Attachment(
        id=attachment_id,
        session_id=session_id,
        filename=filename,
        content_type=store.download_type(filename),
        size=size,
        uploaded_by_id=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{session_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    session_id: str,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get(db, session_id)
    rows = await db.scalars(
        select(Attachment)
        .where(Attachment.session_id == session_id)
        .order_by(Attachment.created_at)
    )
    return list(rows)


@router.get("/{session_id}/attachments/{attachment_id}/download")
async def download_attachment(
    session_id: str,
    attachment_id: str,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _attachment(db, session_id, attachment_id)
    path = store.stored_path(session_id, row.id, row.filename)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The stored file is missing")
    return FileResponse(
        path,
        media_type=row.content_type,
        filename=row.filename,
        headers=_DOWNLOAD_HEADERS,
    )


@router.delete("/{session_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    session_id: str,
    attachment_id: str,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Drop a file from the composer before it is sent."""
    row = await _attachment(db, session_id, attachment_id)
    if row.run_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This file has already been sent. The agent was told where to find it, "
            "so removing it now would change what the transcript says happened.",
        )
    store.discard(session_id, attachment_id)
    await db.delete(row)
    await db.commit()


async def _attachment(db: AsyncSession, session_id: str, attachment_id: str) -> Attachment:
    row = await db.get(Attachment, attachment_id)
    # Checking the session too keeps one conversation's ids from addressing
    # another's, even though they are unguessable.
    if row is None or row.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return row


# --- files the agent produced ---------------------------------------------
async def _files_root(db: AsyncSession, session_id: str) -> Path:
    """The one directory this session's file endpoints may read.

    The session's workspace, which is the directory the agent actually ran in;
    sessions without one fall back to the workspace root, exactly as the runner
    does when it picks a cwd.
    """
    sess = await _get(db, session_id)
    raw = sess.workspace.path if sess.workspace else settings.workspace_root
    return Path(raw).resolve()


@router.get("/{session_id}/files", response_model=SessionFilesOut)
async def list_session_files(
    session_id: str,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    root = await _files_root(db, session_id)
    if not root.is_dir():
        return SessionFilesOut(
            root=str(root),
            files=[],
            truncated=False,
            max_files=settings.session_files_max,
            max_depth=settings.session_files_max_depth,
        )
    found, truncated = store.walk(root)
    return SessionFilesOut(
        root=str(root),
        files=[
            SessionFile(
                path=f.path,
                size=f.size,
                modified=datetime.fromtimestamp(f.modified, tz=timezone.utc),
            )
            for f in found
        ],
        truncated=truncated,
        max_files=settings.session_files_max,
        max_depth=settings.session_files_max_depth,
    )


@router.get("/{session_id}/files/download")
async def download_session_file(
    session_id: str,
    path: str = Query(..., description="Path relative to the session's workspace"),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    root = await _files_root(db, session_id)
    target = store.resolve_inside(root, path)
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    name = store.safe_filename(target.name)
    return FileResponse(
        target,
        media_type=store.download_type(name),
        filename=name,
        headers=_DOWNLOAD_HEADERS,
    )


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
