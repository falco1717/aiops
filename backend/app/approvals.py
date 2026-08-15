from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .events import hub
from .models import Approval, User
from .names import display_name

log = logging.getLogger("aiops.approvals")


@dataclass
class Decision:
    """The answer to one approval request."""

    allowed: bool
    note: str | None = None
    #: Only used by providers that let the approver edit the call before it runs.
    updated_input: dict[str, Any] | None = None


class ApprovalBroker:
    """Holds agent processes while a human answers.

    An agent that asks for permission has genuinely stopped and is waiting on a
    reply, so each pending request owns a future that the API resolves. The
    registry is in-process, which is correct here because the waiting subprocess
    is a child of this same process — but it does mean the app must not be run
    with multiple uvicorn workers.
    """

    def __init__(self) -> None:
        self._waiters: dict[int, asyncio.Future[Decision]] = {}
        self._by_run: dict[int, set[int]] = {}

    async def request(
        self,
        *,
        run_id: int,
        session_id: str,
        provider: str,
        kind: str = "tool",
        tool_name: str | None = None,
        summary: str | None = None,
        request: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Decision:
        """Record a request, tell the browser, and wait for an answer."""
        async with SessionLocal() as db:
            row = Approval(
                run_id=run_id,
                session_id=session_id,
                provider=provider,
                kind=kind,
                tool_name=tool_name,
                summary=(summary or "")[:4000] or None,
                request=request,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            approval_id = row.id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()
        self._waiters[approval_id] = future
        self._by_run.setdefault(run_id, set()).add(approval_id)

        hub.publish(
            session_id,
            {
                "type": "approval.requested",
                "session_id": session_id,
                "run_id": run_id,
                "approval_id": approval_id,
                "provider": provider,
                "kind": kind,
                "tool_name": tool_name,
                "summary": summary,
                "request": request,
            },
        )

        wait = timeout if timeout is not None else settings.approval_timeout_seconds
        try:
            decision = await asyncio.wait_for(asyncio.shield(future), timeout=wait)
        except asyncio.TimeoutError:
            decision = Decision(
                allowed=False,
                note=f"No answer within {int(wait)}s, so it was denied.",
            )
            await self._settle(approval_id, "expired", decision, user_id=None)
        finally:
            self._waiters.pop(approval_id, None)
            self._by_run.get(run_id, set()).discard(approval_id)

        return decision

    async def decide(
        self, approval_id: int, *, allowed: bool, note: str | None, user_id: int | None
    ) -> bool:
        """Answer a pending request. False if it was already settled or is unknown."""
        future = self._waiters.get(approval_id)
        if future is None or future.done():
            return False
        decision = Decision(allowed=allowed, note=note)
        await self._settle(
            approval_id, "allowed" if allowed else "denied", decision, user_id=user_id
        )
        if not future.done():
            future.set_result(decision)
        return True

    async def cancel_run(self, run_id: int) -> None:
        """Release anything still waiting on a run that is going away.

        Without this, cancelling a run that is parked on an approval leaves the
        agent blocked on a future nobody will ever resolve.
        """
        for approval_id in list(self._by_run.get(run_id, ())):
            future = self._waiters.get(approval_id)
            decision = Decision(allowed=False, note="The run was cancelled.")
            await self._settle(approval_id, "cancelled", decision, user_id=None)
            if future is not None and not future.done():
                future.set_result(decision)
        self._by_run.pop(run_id, None)

    def pending_ids(self, run_id: int) -> set[int]:
        return set(self._by_run.get(run_id, ()))

    async def _settle(
        self, approval_id: int, status: str, decision: Decision, *, user_id: int | None
    ) -> None:
        async with SessionLocal() as db:
            row = await db.get(Approval, approval_id)
            if row is None:
                return
            row.status = status
            row.note = decision.note
            row.decided_by_id = user_id
            row.decided_at = datetime.now(timezone.utc)
            await db.commit()
            session_id, run_id = row.session_id, row.run_id
            # Resolved here, while a database session is open, rather than left
            # to the client: a session can be shared, so the other people
            # watching this run see the card vanish and are owed the name of
            # whoever answered it. Null for a timeout or a cancelled run, which
            # nobody decided.
            decider = await db.get(User, user_id) if user_id else None
        hub.publish(
            session_id,
            {
                "type": "approval.resolved",
                "session_id": session_id,
                "run_id": run_id,
                "approval_id": approval_id,
                "status": status,
                "note": decision.note,
                "decided_by_id": user_id,
                "decided_by": display_name(decider) if decider else None,
            },
        )
        log.info("approval %s %s", approval_id, status)


class RunTokens:
    """Short-lived secrets that let a run's approval bridge call back in.

    The bridge is a subprocess of the agent, so it cannot present a user's
    cookie. Instead each run gets a random token, valid only while that run is
    alive, which identifies the run and nothing else — it grants no access to
    any other API.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[int, str]] = {}
        self._by_run: dict[int, str] = {}

    def issue(self, run_id: int, session_id: str) -> str:
        self.revoke(run_id)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (run_id, session_id)
        self._by_run[run_id] = token
        return token

    def resolve(self, token: str) -> tuple[int, str] | None:
        return self._tokens.get(token)

    def revoke(self, run_id: int) -> None:
        token = self._by_run.pop(run_id, None)
        if token:
            self._tokens.pop(token, None)


async def reap_pending_approvals() -> None:
    """Fail any approval left pending by a restart.

    The process that was waiting on it died with the app, so the row would
    otherwise sit "pending" forever and the UI would offer buttons that answer
    nothing.
    """
    async with SessionLocal() as db:
        rows = list(await db.scalars(select(Approval).where(Approval.status == "pending")))
        for row in rows:
            row.status = "expired"
            row.note = "AIOps restarted while this was waiting."
            row.decided_at = datetime.now(timezone.utc)
        if rows:
            await db.commit()
            log.info("Expired %d approval(s) left pending by a restart", len(rows))


broker = ApprovalBroker()
run_tokens = RunTokens()
