from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import attachments as store, handoff
from ..access import can_see_session, sessions_visible_to
from ..config import settings
from ..db import get_db
from ..models import Attachment, Event, Run, Session, SessionShare, Team, User
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
    plan_provider_switch,
    queue_run,
    validate_effort,
    validate_session_targets,
)
from ..skills import discover

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

#: Downloads are user-controlled bytes on the same origin as the session cookie.
#: The Content-Type is already restricted to types a browser will not execute
#: (see attachments.download_type); this stops it guessing a different one.
_DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff"}


async def _get(db: AsyncSession, session_id: str, user: User) -> Session:
    """The session, if this user may see it at all.

    A session they may not see is reported missing rather than forbidden: a 403
    would confirm that this id names a real conversation and invite guessing at
    whose it is.
    """
    sess = await db.get(Session, session_id)
    if sess is None or not await can_see_session(db, sess, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return sess


async def _owned(db: AsyncSession, session_id: str, user: User) -> Session:
    """The session, if this user is the one who may give it away or destroy it."""
    sess = await _get(db, session_id, user)
    if not user.is_admin and sess.owner_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This session was shared with you. Only its owner can share it further, "
            "hand it on, or delete it.",
        )
    return sess


async def _resolve_team(db: AsyncSession, team_id: int | None, user: User) -> None:
    """Check a session may be put in this team before it is.

    Membership is the gate: dropping a session into a team you are not in would
    hand it to a group of people you cannot even enumerate.
    """
    if team_id is None:
        return
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team not found")
    if not user.is_admin and not any(m.user_id == user.id for m in team.members):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"You are not a member of {team.name!r}"
        )


#: Patch fields that change who can reach a session rather than what it does.
SHARING_FIELDS = ("team_id", "shared_user_ids", "owner_id")


async def _active_run_id(db: AsyncSession, session_id: str) -> int | None:
    """The turn this session is in the middle of, if any."""
    return await db.scalar(
        select(Run.id)
        .where(Run.session_id == session_id, Run.status.in_(("queued", "running")))
        .limit(1)
    )


async def _switch_provider(
    db: AsyncSession, sess: Session, data: dict, user: User
) -> None:
    """Move a session to another provider, in place, mid-conversation.

    Not a state transfer, because there is no such thing here: each CLI can only
    resume a session it created itself, so the incoming agent necessarily starts
    a fresh conversation. What carries over is a briefing AIOps writes out of its
    own transcript (handoff.py), owed to the next turn and flagged as such.

    `data` is the patch, and is mutated: the coherence fixes this switch forces
    (an account belonging to the old provider, a model the new CLI has never
    heard of) are folded in underneath whatever the caller asked for explicitly,
    so the caller's own choices still go through the ordinary validation below.
    """
    incoming = data["provider"]
    outgoing = sess.provider

    # Only the owner. Anyone who can see a session can prompt it, but a switch
    # changes which agent every later turn runs on and throws away the resumable
    # session id — that is not a guest's call to make in somebody else's work.
    if not user.is_admin and sess.owner_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only this session's owner can change which agent it runs on. A switch "
            "abandons the provider session behind the conversation, so it is not "
            "something a shared session can have done to it.",
        )

    # Mid-turn there is no answer to "which provider ran this": the prompt has
    # gone to one CLI and the reply would be attributed to another.
    if await _active_run_id(db, sess.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This session is in the middle of a turn. Let it finish or stop it "
            "first — switching now would leave the turn attributed to the wrong "
            "agent and its output unresumable by either.",
        )

    try:
        data.update(
            {
                **await plan_provider_switch(db, sess, incoming, requested=data),
                **data,
            }
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # The old id names a conversation inside the old CLI's own store. The new one
    # cannot load it, and leaving it set would have the runner pass it to
    # `--resume`/`thread/resume` and fail on an id that provider never issued.
    sess.provider_session_id = None
    # available_commands was reported by the outgoing CLI at its startup; the
    # incoming one advertises its own on the next turn.
    sess.available_commands = None
    # No history means nothing to hand over: setting the provider before the
    # first message is just choosing one.
    sess.handoff_pending = await handoff.record_switch(
        db, sess, outgoing=outgoing, incoming=incoming, username=user.username
    )


async def _apply_sharing(
    db: AsyncSession, sess: Session, sharing: dict, user: User
) -> None:
    if "team_id" in sharing:
        await _resolve_team(db, sharing["team_id"], user)
        sess.team_id = sharing["team_id"]

    if "owner_id" in sharing and sharing["owner_id"] != sess.owner_id:
        heir = await db.get(User, sharing["owner_id"]) if sharing["owner_id"] else None
        if heir is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That user does not exist")
        sess.owner_id = heir.id
        # The new owner must not also hold a share of their own session: it
        # would sit in the sharing list with nothing to switch it off.
        for share in list(sess.shares):
            if share.user_id == heir.id:
                sess.shares.remove(share)
                await db.delete(share)

    if sharing.get("shared_user_ids") is not None:
        await _apply_shares(db, sess, sharing["shared_user_ids"])


async def _apply_shares(db: AsyncSession, sess: Session, user_ids: list[int]) -> None:
    for existing in list(sess.shares):
        await db.delete(existing)
    sess.shares = []
    await db.flush()
    for user_id in dict.fromkeys(user_ids):
        # Sharing with the owner is a second, weaker claim on something already
        # theirs, and the UI has no way to show or withdraw it.
        if user_id == sess.owner_id:
            continue
        if await db.get(User, user_id) is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"There is no user with id {user_id}"
            )
        db.add(SessionShare(session_id=sess.id, user_id=user_id))


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    archived: bool = False,
    limit: int = Query(100, le=500),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(Session)
        .where(Session.archived == archived, sessions_visible_to(user))
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
    await _resolve_team(db, payload.team_id, user)
    try:
        sess = await build_session(
            db,
            provider=payload.provider,
            title=payload.title,
            model=payload.model,
            effort=payload.effort,
            preset_id=payload.preset_id,
            workspace_id=payload.workspace_id,
            account_id=payload.account_id,
            approval_mode=payload.approval_mode,
            team_id=payload.team_id,
            user=user,
        )
        await db.commit()
        await db.refresh(sess)
        if payload.prompt:
            await queue_run(
                db,
                sess,
                payload.prompt,
                attachment_ids=payload.attachment_ids,
                requested_by_id=user.id,
            )
            await db.refresh(sess)
    except ValidationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return sess


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    return await _get(db, session_id, user)


@router.patch("/{session_id}", response_model=SessionOut)
async def patch_session(
    session_id: str,
    payload: SessionPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await _get(db, session_id, user)
    data = payload.model_dump(exclude_unset=True)

    # Anyone who can see a session can work in it, but only its owner decides
    # who else gets in and who it belongs to next.
    sharing = {k: data.pop(k) for k in SHARING_FIELDS if k in data}
    if sharing:
        await _owned(db, session_id, user)
        await _apply_sharing(db, sess, sharing, user)

    # A provider change is the one patch that invalidates the rest of the row, so
    # it settles what the session will look like before anything is validated
    # against it. Naming the provider it already runs on is not a switch.
    if data.get("provider") is not None and data["provider"] != sess.provider:
        await _switch_provider(db, sess, data, user)
    provider = data.get("provider") or sess.provider

    # Re-point fields go through the same validation as creation. Without this
    # a user could create a session on an account they may use and then patch
    # it onto a restricted one, and a bad id would surface as a 500 from the
    # foreign key rather than a 400.
    try:
        await validate_session_targets(
            db,
            provider=provider,
            account_id=data.get("account_id", sess.account_id),
            preset_id=data.get("preset_id", sess.preset_id),
            workspace_id=data.get("workspace_id", sess.workspace_id),
            user=user,
        )
        # Against the model this patch leaves behind, not the one it replaces:
        # switching to a model with a shorter effort list has to be caught here.
        validate_effort(
            provider,
            data.get("model", sess.model),
            data.get("effort", sess.effort),
        )
    except ValidationError as exc:
        await db.rollback()
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
    session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    sess = await _owned(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full conversation history. `since_event_id` lets a reconnecting client
    fetch only what it missed while the websocket was down."""
    sess = await _get(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await _get(db, session_id, user)
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
        return await queue_run(
            db,
            sess,
            payload.prompt,
            attachment_ids=payload.attachment_ids,
            requested_by_id=user.id,
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/{session_id}/runs", response_model=list[RunOut])
async def list_runs(
    session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await _get(db, session_id, user)
    rows = await db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.id))
    return list(rows)


@router.get("/{session_id}/events", response_model=list[EventOut])
async def list_events(
    session_id: str,
    run_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get(db, session_id, user)
    stmt = select(Event).where(Event.session_id == session_id)
    if run_id is not None:
        stmt = stmt.where(Event.run_id == run_id)
    rows = await db.scalars(stmt.order_by(Event.id))
    return list(rows)


@router.get("/{session_id}/capabilities", response_model=list[CapabilityOut])
async def capabilities(
    session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Skills and slash commands this session can use.

    These already work by typing `/name` into the prompt; this endpoint just
    lets the composer show what exists rather than relying on memory.
    """
    sess = await _get(db, session_id, user)
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
    await _get(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _attachment(db, session_id, attachment_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Drop a file from the composer before it is sent."""
    row = await _attachment(db, session_id, attachment_id, user)
    if row.run_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This file has already been sent. The agent was told where to find it, "
            "so removing it now would change what the transcript says happened.",
        )
    store.discard(session_id, attachment_id)
    await db.delete(row)
    await db.commit()


async def _attachment(
    db: AsyncSession, session_id: str, attachment_id: str, user: User
) -> Attachment:
    await _get(db, session_id, user)
    row = await db.get(Attachment, attachment_id)
    # Checking the session too keeps one conversation's ids from addressing
    # another's, even though they are unguessable.
    if row is None or row.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return row


# --- files the agent produced ---------------------------------------------
async def _files_root(db: AsyncSession, session_id: str, user: User) -> Path:
    """The one directory this session's file endpoints may read.

    The session's workspace, which is the directory the agent actually ran in;
    sessions without one fall back to the workspace root, exactly as the runner
    does when it picks a cwd.
    """
    sess = await _get(db, session_id, user)
    raw = sess.workspace.path if sess.workspace else settings.workspace_root
    return Path(raw).resolve()


@router.get("/{session_id}/files", response_model=SessionFilesOut)
async def list_session_files(
    session_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    root = await _files_root(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    root = await _files_root(db, session_id, user)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get(db, session_id, user)
    event = await db.get(Event, event_id)
    if event is None or event.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return {"raw": event.raw}
