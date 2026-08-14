from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import can_see_session, sessions_visible_to
from ..db import get_db
from ..models import ProviderAccount, Run, Session, User
from ..schemas import SessionContextOut, UsageOut, UsageWindow
from ..security import current_user

router = APIRouter(prefix="/api/usage", tags=["usage"])

# These windows are measured from what AIOps has run — "how hard have I leaned
# on this account here". The authoritative plan allowance is separate: Claude
# Code emits a rate_limit_event carrying the real window state, which is stored
# on the account and surfaced alongside these figures.
WINDOWS = [
    ("Last 5 hours", timedelta(hours=5)),
    ("Last 24 hours", timedelta(days=1)),
    ("Last 7 days", timedelta(days=7)),
]

NOTE = (
    "Counted from runs AIOps executed in sessions you can see, so it covers this "
    "server only and your own work only — turns in somebody else's conversation, "
    "and anything you do in a terminal elsewhere on the same account, are not "
    "included. Plan windows above come from the CLI itself and are "
    "authoritative. Token costs are API-rate estimates and are not billed "
    "separately on a subscription."
)


def _cols():
    return (
        func.count(Run.id),
        func.coalesce(func.sum(Run.input_tokens), 0),
        func.coalesce(func.sum(Run.output_tokens), 0),
        func.coalesce(func.sum(Run.cache_read_tokens), 0),
        func.coalesce(func.sum(Run.cache_write_tokens), 0),
        func.coalesce(func.sum(Run.cost_usd), 0.0),
    )


@router.get("", response_model=UsageOut)
async def usage(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """What this user has spent, counted over the conversations they can see.

    A run is a turn of a session, so it is exactly as visible as that session is.
    Unscoped, these figures reported how much work everybody on the instance had
    done and — with the per-account breakdown below — on whose account, to anyone
    who loaded the page.

    The account roster itself is deliberately *not* scoped. `GET /api/accounts`
    already lists every account with its name, provider and limit state to every
    signed-in user, because you have to be able to see the accounts to choose one
    for a session, and a limited account is the reason someone else's run just
    failed over onto yours. What was never instance-level is the usage: an
    account you can see but have not driven now reads as zero rather than as
    somebody else's totals.
    """
    now = datetime.now(timezone.utc)
    mine = Run.session_id.in_(select(Session.id).where(sessions_visible_to(user)))
    windows: list[UsageWindow] = []
    for label, delta in WINDOWS:
        since = now - delta
        row = (
            await db.execute(select(*_cols()).where(Run.created_at >= since, mine))
        ).one()
        runs, inp, outp, cread, cwrite, cost = row
        windows.append(
            UsageWindow(
                label=label,
                since=since,
                runs=runs or 0,
                input_tokens=int(inp),
                output_tokens=int(outp),
                cache_read_tokens=int(cread),
                cache_write_tokens=int(cwrite),
                total_tokens=int(inp) + int(outp) + int(cread) + int(cwrite),
                cost_usd=float(cost or 0),
            )
        )

    since = now - timedelta(days=7)
    rows = (
        await db.execute(
            select(
                ProviderAccount.id,
                ProviderAccount.name,
                ProviderAccount.provider,
                ProviderAccount.limited_until,
                *_cols(),
            )
            # Every condition on the run belongs in the ON clause, not a WHERE.
            # This is an outer join whose whole point is to keep accounts with
            # nothing to count, and a WHERE would drop those rows back out again
            # — so an account somebody else has been hammering would vanish from
            # your list entirely instead of showing zero, which is a slower way
            # of leaking the same fact.
            .join(
                Run,
                (Run.account_id == ProviderAccount.id)
                & (Run.created_at >= since)
                & mine,
                isouter=True,
            )
            .group_by(
                ProviderAccount.id,
                ProviderAccount.name,
                ProviderAccount.provider,
                ProviderAccount.limited_until,
            )
            .order_by(ProviderAccount.name)
        )
    ).all()

    by_account = [
        {
            "account_id": r[0],
            "name": r[1],
            "provider": r[2],
            "limited_until": r[3].isoformat() if r[3] else None,
            "runs": r[4] or 0,
            "total_tokens": int(r[5]) + int(r[6]) + int(r[7]) + int(r[8]),
            "cost_usd": float(r[9] or 0),
        }
        for r in rows
    ]

    return UsageOut(windows=windows, by_account=by_account, note=NOTE)


@router.get("/session/{session_id}", response_model=SessionContextOut)
async def session_usage(
    session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Context pressure for one conversation.

    `last_context_tokens` is what the model had to read on the most recent turn,
    which is the number that grows as a conversation gets long.
    """
    sess = await db.get(Session, session_id)
    if sess is None or not await can_see_session(db, sess, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    row = (
        await db.execute(
            select(
                func.count(Run.id),
                func.coalesce(func.sum(Run.input_tokens), 0),
                func.coalesce(func.sum(Run.output_tokens), 0),
                func.coalesce(func.sum(Run.cache_read_tokens), 0),
                func.coalesce(func.sum(Run.cache_write_tokens), 0),
                func.coalesce(func.sum(Run.cost_usd), 0.0),
                func.max(Run.context_tokens),
            ).where(Run.session_id == session_id)
        )
    ).one()
    last = await db.scalar(
        select(Run.context_tokens)
        .where(Run.session_id == session_id, Run.context_tokens.isnot(None))
        .order_by(Run.id.desc())
        .limit(1)
    )
    return SessionContextOut(
        session_id=session_id,
        last_context_tokens=last,
        peak_context_tokens=row[6],
        total_tokens=int(row[1]) + int(row[2]) + int(row[3]) + int(row[4]),
        runs=row[0] or 0,
        cost_usd=float(row[5] or 0),
    )
