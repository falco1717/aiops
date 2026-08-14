from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AgentPreset, Attachment, ProviderAccount, Run, Session, User, Workspace
from .providers import PROVIDERS
from .runner import runner


class ValidationError(ValueError):
    pass


#: How a session handles tool permissions. "ask" pauses the agent and puts the
#: decision in the UI; "auto" approves file edits without asking; "bypass"
#: turns permission checks off entirely.
APPROVAL_MODES = ("ask", "auto", "bypass")


async def validate_session_targets(
    db: AsyncSession,
    *,
    provider: str,
    account_id: int | None,
    preset_id: int | None,
    workspace_id: int | None,
    user: User | None = None,
) -> AgentPreset | None:
    """Check that an account/preset/workspace may be used with this provider.

    Shared by session creation and by re-pointing an existing session, so both
    paths enforce account access identically.
    """
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

    return preset


async def build_session(
    db: AsyncSession,
    *,
    provider: str,
    title: str | None,
    model: str | None,
    preset_id: int | None,
    workspace_id: int | None,
    account_id: int | None = None,
    approval_mode: str | None = None,
    team_id: int | None = None,
    # Set explicitly by callers with no request user behind them — the
    # scheduler, which owns its sessions on behalf of whoever wrote the job.
    owner_id: int | None = None,
    user: User | None = None,
) -> Session:
    """Create (but do not commit) a session, resolving preset and workspace."""
    if approval_mode is not None and approval_mode not in APPROVAL_MODES:
        raise ValidationError(
            f"approval_mode must be one of {', '.join(APPROVAL_MODES)} (got {approval_mode!r})"
        )
    preset = await validate_session_targets(
        db,
        provider=provider,
        account_id=account_id,
        preset_id=preset_id,
        workspace_id=workspace_id,
        user=user,
    )

    sess = Session(
        title=(title or "Untitled").strip()[:255] or "Untitled",
        provider=provider,
        model=model or (preset.model if preset else None),
        preset_id=preset_id,
        workspace_id=workspace_id,
        account_id=account_id,
        approval_mode=approval_mode,
        team_id=team_id,
        owner_id=owner_id if owner_id is not None else (user.id if user else None),
    )
    db.add(sess)
    return sess


async def queue_run(
    db: AsyncSession,
    session: Session,
    prompt: str,
    *,
    schedule_id: int | None = None,
    attachment_ids: list[str] | None = None,
    requested_by_id: int | None = None,
) -> Run:
    """Persist a turn and hand it to the runner. Commits."""
    prompt = prompt.strip()
    if not prompt:
        raise ValidationError("Prompt must not be empty")

    attached = await _claimable_attachments(db, session.id, attachment_ids or [])

    run = Run(
        session_id=session.id,
        prompt=prompt,
        schedule_id=schedule_id,
        requested_by_id=requested_by_id,
        status="queued",
    )
    db.add(run)
    await db.flush()
    for row in attached:
        row.run_id = run.id
    session.status = "running"
    session.updated_at = datetime.now(timezone.utc)
    if session.title == "Untitled":
        session.title = prompt.splitlines()[0][:80]
    await db.commit()
    await db.refresh(run)

    runner.submit(run.id)
    return run


async def _claimable_attachments(
    db: AsyncSession, session_id: str, ids: list[str]
) -> list[Attachment]:
    """The uploads this turn may claim.

    Scoped to the session so an id from someone else's conversation cannot be
    attached here, and to unsent rows so a file cannot be re-pointed at a later
    turn once the agent has already been told where it is.
    """
    if not ids:
        return []
    rows = list(
        await db.scalars(
            select(Attachment).where(
                Attachment.id.in_(ids),
                Attachment.session_id == session_id,
                Attachment.run_id.is_(None),
            )
        )
    )
    missing = set(ids) - {row.id for row in rows}
    if missing:
        raise ValidationError(
            f"{len(missing)} attachment(s) are not available to this message. "
            "They may have been removed, or already sent with an earlier turn."
        )
    return rows
