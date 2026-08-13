from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Schedule, User
from ..providers import PROVIDERS
from ..scheduler import compute_next_run, fire_schedule, validate_cron
from ..schemas import ScheduleIn, ScheduleOut
from ..security import current_user

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _validate(payload: ScheduleIn) -> None:
    if payload.provider not in PROVIDERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown provider {payload.provider!r}. Known: {', '.join(PROVIDERS)}",
        )
    if payload.session_mode not in ("new", "continue"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "session_mode must be 'new' or 'continue'")
    try:
        validate_cron(payload.cron, payload.timezone_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(Schedule).order_by(Schedule.name)))


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleIn, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    _validate(payload)
    if await db.scalar(select(Schedule).where(Schedule.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A schedule with that name already exists")
    sched = Schedule(**payload.model_dump())
    sched.next_run_at = compute_next_run(sched)
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return sched


@router.put("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: int,
    payload: ScheduleIn,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate(payload)
    sched = await db.get(Schedule, schedule_id)
    if sched is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    for key, value in payload.model_dump().items():
        setattr(sched, key, value)
    sched.next_run_at = compute_next_run(sched)
    await db.commit()
    await db.refresh(sched)
    return sched


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    sched = await db.get(Schedule, schedule_id)
    if sched is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    await db.delete(sched)
    await db.commit()


@router.post("/{schedule_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_now(
    schedule_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Fire a schedule immediately without touching its cron timing."""
    sched = await db.get(Schedule, schedule_id)
    if sched is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    run = await fire_schedule(db, sched, advance_next=False)
    return {"run_id": run.id, "session_id": run.session_id}
