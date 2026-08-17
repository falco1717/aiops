from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import attachments as store, browsing
from ..access import can_see_session, sessions_visible_to
from ..db import get_db
from ..models import Event, Run, Session, User
from ..names import display_name
from ..runner import runner, settle_session
from ..schemas import ActiveRunOut, RunOut, WorkEvent
from ..security import current_user

router = APIRouter(prefix="/api/runs", tags=["runs"])

#: How many of a turn's most recent steps travel with the active-work summary.
#: Enough for the client to say what is happening now and which background tasks
#: are still open under it, and no more: this is polled from every screen.
RECENT_STEPS = 25

#: How much of one step's text goes with it. The client shows the first line of
#: it and nothing else, so this only has to be longer than a line.
STEP_TEXT_CHARS = 240

#: How much of the opening message identifies a row in the list.
PROMPT_CHARS = 160


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


@router.get("/active", response_model=list[ActiveRunOut])
async def active_runs(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Every turn still in flight in a session this user can see.

    Declared above `/{run_id}` deliberately: FastAPI matches routes in the order
    they are registered, and while `run_id: int` would reject the word "active"
    rather than swallow it, it would answer 422 instead of this.

    The scope is `sessions_visible_to` and nothing else. An administrator sees
    exactly their own work here — the asymmetry is the point, and a "what is
    everyone running" view is precisely what that rule exists to prevent, so
    this must never grow one.

    Both queued and running turns are returned. A message waiting behind
    somebody else's turn is in flight as far as the person who sent it is
    concerned, and leaving it out would make the indicator disagree with the
    session it is pointing at.
    """
    rows = list(
        await db.execute(
            select(Run, Session.title)
            .join(Session, Session.id == Run.session_id)
            .where(Run.status.in_(("queued", "running")), sessions_visible_to(user))
            # Running turns first, then the queue in the order it will run —
            # which is by id, the same order the dispatcher takes them in.
            # Spelled out rather than relying on "running" sorting after
            # "queued", which is true of these two words and of nothing else.
            .order_by(case((Run.status == "running", 0), else_=1), Run.id)
        )
    )
    if not rows:
        return []

    # One lookup for every sender named in the list rather than one per row: a
    # shared session's queue is often several turns from the same person.
    sender_ids = {run.requested_by_id for run, _ in rows if run.requested_by_id is not None}
    senders = {
        u.id: display_name(u)
        for u in (
            await db.scalars(select(User).where(User.id.in_(sender_ids)))
            if sender_ids
            else []
        )
    }

    out: list[ActiveRunOut] = []
    for run, title in rows:
        out.append(
            ActiveRunOut(
                run_id=run.id,
                session_id=run.session_id,
                session_title=title,
                status=run.status,
                provider=run.provider,
                prompt=run.prompt[:PROMPT_CHARS],
                requested_by_id=run.requested_by_id,
                requested_by=senders.get(run.requested_by_id or -1),
                created_at=run.created_at,
                started_at=run.started_at,
                tools=await _tool_calls(db, run.id),
                recent=await _recent_steps(db, run.id),
            )
        )
    return out


async def _tool_calls(db: AsyncSession, run_id: int) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.run_id == run_id, Event.kind == "tool_use")
        )
    ) or 0


async def _recent_steps(db: AsyncSession, run_id: int) -> list[WorkEvent]:
    """The tail of a run's events, oldest last, with the text cut short.

    Read newest-first and then reversed, so the database can answer it off the
    (run_id, seq) index without sorting a turn that has produced ten thousand
    events.
    """
    rows = list(
        await db.scalars(
            select(Event)
            .where(Event.run_id == run_id)
            .order_by(desc(Event.seq))
            .limit(RECENT_STEPS)
        )
    )
    rows.reverse()
    return [
        WorkEvent(
            run_id=e.run_id,
            seq=e.seq,
            kind=e.kind,
            text=None if e.text is None else e.text[:STEP_TEXT_CHARS],
            tool_name=e.tool_name,
            parent_tool_use_id=e.parent_tool_use_id,
            agent_name=e.agent_name,
        )
        for e in rows
    ]


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
    being administrators, which is deliberate. That check runs first, so who may
    ask is settled before anything on disk is looked for: an outsider gets the
    same 404 whether the capture exists or not.

    Addressed by run because that is where it sits in the transcript, but stored
    with the *session* and living exactly as long as one — the same volume, and
    the same deletion, as a file the operator attached to a message. Reopening a
    finished conversation shows what its browser saw, which is the whole reason
    anybody wants these.

    A 404 is therefore an ordinary answer rather than a fault: a turn that ran
    before AIOps kept them has none, and a capture too large for the session's
    budget was declined at the time. The client draws a caption, not a broken
    image.

    Served under exactly the rules an attachment is: a content type off the list
    of things a browser will not execute, `nosniff` so it cannot guess past
    that, and a download disposition. The bytes came off a page the agent chose,
    and they arrive on the same origin as the session cookie.
    """
    run = await _get(db, run_id, user)
    path = browsing.stored_shot(run.session_id, run_id, name)
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That screenshot is not available. Captures are kept with the conversation "
            "and deleted with it; a turn that ran before AIOps kept them has none.",
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
