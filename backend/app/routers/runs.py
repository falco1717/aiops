from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Run, Session, User
from ..runner import runner
from ..schemas import RunOut
from ..security import current_user

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Run).order_by(desc(Run.id)).limit(limit)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    return list(await db.scalars(stmt))


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Terminate the agent process and every child it spawned."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
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
            sess = await db.get(Session, run.session_id)
            if sess is not None and sess.status == "running":
                sess.status = "idle"
            await db.commit()
        else:
            return {"status": run.status, "detail": "Run had already finished"}
    return {"status": "cancelling"}
