from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text, update

from .config import settings
from .db import SessionLocal, engine
from .models import User

log = logging.getLogger("aiops.migrate")

# Additive-only column migrations. `create_all` creates missing *tables* but
# never alters an existing one, so a deployed database keeps its old shape
# after a model change unless we do this.
COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "is_admin": "BOOLEAN NOT NULL DEFAULT FALSE",
        "must_change_password": "BOOLEAN NOT NULL DEFAULT FALSE",
        "last_login_at": "TIMESTAMP WITH TIME ZONE",
    },
}

# SQLite (used in development) spells a few types differently.
SQLITE_TYPES = {
    "BOOLEAN NOT NULL DEFAULT FALSE": "BOOLEAN NOT NULL DEFAULT 0",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
}


async def run_migrations() -> None:
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        existing_tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

        for table, columns in COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all already made it with every column
            present = await conn.run_sync(
                lambda c, t=table: {col["name"] for col in inspect(c).get_columns(t)}
            )
            for name, ddl in columns.items():
                if name in present:
                    continue
                if dialect == "sqlite":
                    ddl = SQLITE_TYPES.get(ddl, ddl)
                await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
                log.info("migration: added %s.%s", table, name)

    await _ensure_an_admin_exists()


async def _ensure_an_admin_exists() -> None:
    """Never leave an instance with no one able to manage users.

    On an upgrade every existing row defaults to is_admin=false, which would
    lock the operator out of the new user-management screens.
    """
    async with SessionLocal() as db:
        if await db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)):
            return
        target = await db.scalar(
            select(User).where(User.username == settings.admin_username)
        ) or await db.scalar(select(User).order_by(User.id).limit(1))
        if target is None:
            return  # empty install; bootstrap will create the admin
        await db.execute(update(User).where(User.id == target.id).values(is_admin=True))
        await db.commit()
        log.info("migration: promoted %r to admin", target.username)
