from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AgentPreset, ProviderAccount, Run, Session, User, Workspace
from .providers import PROVIDERS
from .runner import runner


class ValidationError(ValueError):
    pass


async def build_session(
    db: AsyncSession,
    *,
    provider: str,
    title: str | None,
    model: str | None,
    preset_id: int | None,
    workspace_id: int | None,
    account_id: int | None = None,
    user: User | None = None,
) -> Session:
    """Create (but do not commit) a session, resolving preset and workspace."""
    if provider not in PROVIDERS:
        raise ValidationError(f"Unknown provider {provider!r}. Known: {', '.join(PROVIDERS)}")

    if account_id is not None:
        account = await db.get(ProviderAccount, account_id)
        if account is None:
            raise ValidationError("Account not found")
        if account.provider != provider:
            raise ValidationError(
                f"Account {account.name!r} is for {account.provider!r}, not {provider!r}"
            )
        # An ungranted account is open to everyone; once anyone is granted, only
        # they (and admins) may use it.
        if user is not None and not user.is_admin and account.grants:
            if not any(g.user_id == user.id for g in account.grants):
                raise ValidationError(f"You do not have access to the account {account.name!r}")

    preset = None
    if preset_id is not None:
        preset = await db.get(AgentPreset, preset_id)
        if preset is None:
            raise ValidationError("Preset not found")
        if preset.provider != provider:
            raise ValidationError(
                f"Preset {preset.name!r} is for provider {preset.provider!r}, not {provider!r}"
            )

    if workspace_id is not None and await db.get(Workspace, workspace_id) is None:
        raise ValidationError("Workspace not found")

    sess = Session(
        title=(title or "Untitled").strip()[:255] or "Untitled",
        provider=provider,
        model=model or (preset.model if preset else None),
        preset_id=preset_id,
        workspace_id=workspace_id,
        account_id=account_id,
    )
    db.add(sess)
    return sess


async def queue_run(
    db: AsyncSession,
    session: Session,
    prompt: str,
    *,
    schedule_id: int | None = None,
) -> Run:
    """Persist a turn and hand it to the runner. Commits."""
    prompt = prompt.strip()
    if not prompt:
        raise ValidationError("Prompt must not be empty")

    run = Run(session_id=session.id, prompt=prompt, schedule_id=schedule_id, status="queued")
    db.add(run)
    session.status = "running"
    session.updated_at = datetime.now(timezone.utc)
    if session.title == "Untitled":
        session.title = prompt.splitlines()[0][:80]
    await db.commit()
    await db.refresh(run)

    runner.submit(run.id)
    return run
