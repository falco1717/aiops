from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .models import Run, Schedule, Session
from .services import build_session, queue_run

log = logging.getLogger("aiops.scheduler")


def validate_cron(expression: str, timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {timezone_name!r}") from exc
    try:
        croniter(expression)
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression {expression!r}: {exc}") from exc


def compute_next_run(schedule: Schedule, after: datetime | None = None) -> datetime | None:
    if not schedule.enabled:
        return None
    try:
        tz = ZoneInfo(schedule.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    base = (after or datetime.now(timezone.utc)).astimezone(tz)
    try:
        return croniter(schedule.cron, base).get_next(datetime).astimezone(timezone.utc)
    except (CroniterBadCronError, ValueError):
        log.warning("schedule %s has an invalid cron expression", schedule.name)
        return None


async def fire_schedule(db: AsyncSession, schedule: Schedule, *, advance_next: bool) -> Run:
    """Create or reuse a session for this schedule and queue its prompt."""
    session: Session | None = None
    if schedule.session_mode == "continue" and schedule.target_session_id:
        session = await db.get(Session, schedule.target_session_id)

    if session is None:
        session = await build_session(
            db,
            provider=schedule.provider,
            title=f"{schedule.name} — {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
            model=schedule.model,
            preset_id=schedule.preset_id,
            workspace_id=schedule.workspace_id,
            account_id=schedule.account_id,
            owner_id=schedule.owner_id,
        )
        await db.commit()
        await db.refresh(session)
        if schedule.session_mode == "continue":
            schedule.target_session_id = session.id

    run = await queue_run(db, session, schedule.prompt, schedule_id=schedule.id)

    now = datetime.now(timezone.utc)
    schedule.last_run_at = now
    schedule.last_status = "queued"
    if advance_next:
        schedule.next_run_at = compute_next_run(schedule, after=now)
    await db.commit()
    return run


async def scheduler_loop() -> None:
    """Poll for due schedules. One tick per `scheduler_tick_seconds`."""
    log.info("scheduler started (tick=%ss)", settings.scheduler_tick_seconds)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one bad schedule stop the loop
            log.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_tick_seconds)


async def _tick() -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        due = list(
            await db.scalars(
                select(Schedule).where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.isnot(None),
                    Schedule.next_run_at <= now,
                )
            )
        )
        for schedule in due:
            # Recompute first so a failure below can't cause a tight retry loop.
            schedule.next_run_at = compute_next_run(schedule, after=now)
            await db.commit()
            try:
                await fire_schedule(db, schedule, advance_next=False)
                log.info("fired schedule %s", schedule.name)
            except Exception as exc:  # noqa: BLE001
                log.exception("schedule %s failed to fire", schedule.name)
                # The failure may have left the session needing a rollback; if
                # so, the recovery commit below would itself raise and abort
                # every remaining due schedule in this tick.
                await db.rollback()
                schedule.last_status = f"error: {type(exc).__name__}"[:32]
                await db.commit()


async def backfill_next_runs() -> None:
    """On boot, give every enabled schedule a next_run_at."""
    async with SessionLocal() as db:
        rows = list(await db.scalars(select(Schedule).where(Schedule.enabled.is_(True))))
        changed = False
        for schedule in rows:
            if schedule.next_run_at is None:
                schedule.next_run_at = compute_next_run(schedule)
                changed = True
        if changed:
            await db.commit()
