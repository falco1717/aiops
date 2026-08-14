from __future__ import annotations

import logging
import os

from sqlalchemy import inspect, select, text, update

from .config import settings
from .db import SessionLocal, engine
from .models import ProviderAccount, User

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
    "sessions": {
        "account_id": "INTEGER",
        "available_commands": "JSON",
        "approval_mode": "VARCHAR(32)",
        "owner_id": "INTEGER",
    },
    "targets": {
        "owner_id": "INTEGER",
    },
    "target_access": {
        "level": "VARCHAR(16) NOT NULL DEFAULT 'use'",
    },
    "provider_accounts": {
        "limit_status": "VARCHAR(32)",
        "limit_window": "VARCHAR(32)",
        "limit_resets_at": "TIMESTAMP WITH TIME ZONE",
        "limit_seen_at": "TIMESTAMP WITH TIME ZONE",
    },
    "schedules": {
        "account_id": "INTEGER",
    },
    "runs": {
        "account_id": "INTEGER",
        "failed_over_from_id": "INTEGER",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "cache_read_tokens": "INTEGER",
        "cache_write_tokens": "INTEGER",
        "context_tokens": "INTEGER",
    },
    "events": {
        "parent_tool_use_id": "VARCHAR(128)",
        "agent_name": "VARCHAR(128)",
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
    await _adopt_existing_logins()
    await _backfill_owners()


# Where each CLI keeps credentials when no per-account directory is set. An
# install that signed in before named accounts existed has its login here.
LEGACY_DIRS = {
    "claude": os.path.join(os.path.expanduser("~"), ".claude"),
    "codex": os.path.join(os.path.expanduser("~"), ".codex"),
}


async def _adopt_existing_logins() -> None:
    """Turn a pre-existing CLI login into a named account.

    Without this, adding the accounts feature would appear to log the operator
    out: the runner would start pointing at fresh per-account directories and
    ignore the credentials already on disk.
    """
    async with SessionLocal() as db:
        if await db.scalar(select(ProviderAccount.id).limit(1)):
            return
        for provider, path in LEGACY_DIRS.items():
            if not os.path.isdir(path):
                continue
            db.add(
                ProviderAccount(
                    name=f"Default {provider.capitalize()}",
                    provider=provider,
                    slug=f"default-{provider}",
                    description="Adopted from the credentials already on this server.",
                    config_dir=path,
                    is_default=True,
                )
            )
            log.info("migration: adopted existing %s login at %s", provider, path)
        await db.commit()


async def _backfill_owners() -> None:
    """Give pre-ownership rows an owner.

    Sessions and stored systems used to be visible to everyone, so nothing
    recorded who made them. Anything without an owner would be invisible to
    every user once visibility starts depending on one, so it is assigned to
    an administrator, who can hand it on. This is logged loudly because it is
    a visibility change to existing data, not a schema detail.
    """
    async with SessionLocal() as db:
        admin = await db.scalar(
            select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1)
        )
        if admin is None:
            return
        for table, label in (("sessions", "session"), ("targets", "stored system")):
            result = await db.execute(
                text(f"UPDATE {table} SET owner_id = :owner WHERE owner_id IS NULL"),
                {"owner": admin.id},
            )
            if result.rowcount:
                log.warning(
                    "migration: %d %s(s) had no owner and are now owned by %r. "
                    "They are no longer visible to other users until shared.",
                    result.rowcount,
                    label,
                    admin.username,
                )
        # A grant to the owner is meaningless — they already have every right —
        # but rows predating ownership can hold one, and it surfaces as a system
        # "shared with" its own owner that the UI offers no way to switch off,
        # because it never lists you against your own systems.
        result = await db.execute(
            text(
                "DELETE FROM target_access WHERE user_id IN "
                "(SELECT owner_id FROM targets WHERE targets.id = target_access.target_id)"
            )
        )
        if result.rowcount:
            log.info("migration: dropped %d self-grant(s) on stored systems", result.rowcount)
        await db.commit()


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
