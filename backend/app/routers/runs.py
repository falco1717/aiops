from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import attachments as store, browsing
from ..access import can_see_session, sessions_visible_to
from ..db import get_db
from ..models import Run, Session, User
from ..runner import runner, settle_session
from ..schemas import RunOut
from ..security import current_user

router = APIRouter(prefix="/api/runs", tags=["runs"])


async def _get(db: AsyncSession, run_id: int, user: User) -> Run:
    """A run is a turn of a conversation, so it is only as visible as that
    conversation is — including the prompt it carries."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    sess = await db.get(Session, run.session_id)
    if sess is None or not await can_see_session(db, sess, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.get("", response_model=list[RunOut])
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Run)
        .where(Run.session_id.in_(select(Session.id).where(sessions_visible_to(user))))
        .order_by(desc(Run.id))
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    return list(await db.scalars(stmt))


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    return await _get(db, run_id, user)


@router.get("/{run_id}/screenshots/{name}")
async def run_screenshot(
    run_id: int,
    name: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """One photograph the agent's browser took during this turn.

    Session content, so it is visible to exactly the people the transcript it
    sits in is visible to — `_get` above is the same `can_see_session` rule the
    prompt and the events go through, and administrators get nothing extra by
    being administrators, which is deliberate.

    Addressed by run rather than by session because that is what it belongs to:
    a screenshot exists for the length of one turn and is deleted with it, in
    the same `finally` that removes the run's decrypted ssh materials. There is
    no second copy anywhere with a longer life, which is why a finished turn
    answers 404 here — the record that a screenshot was taken stays in the
    transcript, the pixels do not outlive the turn that needed them.

    Served under exactly the rules an attachment is: a content type off the list
    of things a browser will not execute, `nosniff` so it cannot guess past
    that, and a download disposition. The bytes came off a page the agent chose,
    and they arrive on the same origin as the session cookie.
    """
    await _get(db, run_id, user)
    path = browsing.screenshot_path(run_id, name)
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That screenshot is not available. Screenshots live for the length of "
            "the turn that took them and are deleted with it.",
        )
    return FileResponse(
        path,
        media_type=store.download_type(name),
        filename=name,
        headers=store.DOWNLOAD_HEADERS,
    )


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Terminate the agent process and every child it spawned."""
    run = await _get(db, run_id, user)
    if run.status not in ("queued", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run already {run.status}")
    if not await runner.cancel(run_id):
        # Not tracked in this process. Either a leftover from a restart, or the
        # run finished in the moment between the check above and this call —
        # re-read before overwriting, or a cancel that arrives just too late
        # turns a successful run into a cancelled one.
        await db.refresh(run)
        if run.status in ("queued", "running"):
            run.status = "cancelled"
            run.error = "Cancelled; no live process was found for this run"
            run.finished_at = datetime.now(timezone.utc)
            # Not a flat "idle": the session may still have queued messages
            # behind this one, and calling it idle would have the composer and
            # the session list both claim the agent had stopped.
            await settle_session(db, run.session_id)
            await db.commit()
        else:
            return {"status": run.status, "detail": "Run had already finished"}
    return {"status": "cancelling"}


@router.post("/{run_id}/withdraw", status_code=status.HTTP_202_ACCEPTED)
async def withdraw_run(
    run_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Take a message back out of the queue, before anything has run it.

    Deliberately narrower than `/cancel`: it refuses the moment the turn is the
    one in flight. Unsending a message that has not been read is a different act
    from killing an agent mid-command — the second discards work and can leave a
    half-finished edit on disk — so the two are not one button that quietly does
    whichever applies.
    """
    run = await _get(db, run_id, user)
    if run.status != "queued":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This turn is {run.status}, not waiting in the queue."
            if run.status != "running"
            else "The agent has already started this turn. Use Stop to end it.",
        )
    if not await runner.withdraw(run_id, run.session_id):
        # Lost the race with the drain, which starts turns under the same lock
        # this just failed to win: it is running now, and stopping a live agent
        # is not what was asked for.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The agent picked this message up while you were withdrawing it. "
            "Use Stop to end the turn.",
        )
    return {"status": "cancelled"}
