from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Run, User
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
        # Not tracked in this process — most likely a leftover from a restart.
        run.status = "cancelled"
        await db.commit()
    return {"status": "cancelling"}
