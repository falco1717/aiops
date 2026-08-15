from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .events import hub
from .models import AgentPreset, Attachment, ProviderAccount, Run, Session, User, Workspace
from .providers import PROVIDERS
from .runner import runner


class ValidationError(ValueError):
    pass


#: How a session handles tool permissions. "ask" pauses the agent and puts the
#: decision in the UI; "auto" approves file edits without asking; "bypass"
#: turns permission checks off entirely.
APPROVAL_MODES = ("ask", "auto", "bypass")


def validate_effort(provider: str, model: str | None, effort: str | None) -> None:
    """Reject a reasoning level the CLI would not act on.

    Both CLIs take a bad level quietly — Claude warns and falls back to its
    default, Codex accepts the config key and only fails once the turn is
    already running — so "high" on a model that stops at "xhigh" would look
    like it worked and silently be something else.
    """
    if not effort:
        return
    adapter = PROVIDERS.get(provider)
    if adapter is None:
        return  # the provider itself is reported by the caller's own check
    allowed = adapter.effort_choices(model)
    if not allowed:
        raise ValidationError(f"The {provider} CLI has no reasoning-effort control")
    if effort not in allowed:
        where = f"{provider} {model}" if model else provider
        raise ValidationError(
            f"{where} accepts effort: {', '.join(allowed)} (got {effort!r})"
        )


async def plan_provider_switch(
    db: AsyncSession,
    sess: Session,
    new_provider: str,
    *,
    requested: dict,
) -> dict:
    """What a session must also change to be coherent on another provider.

    Almost nothing about a session survives a provider change. An account is one
    provider's sign-in; a preset pins a model, a permission mode and usually a
    system prompt in one provider's vocabulary; the two model lists do not
    overlap at a single name; and which reasoning levels exist depends on the
    model. Left alone, a switched session would name an account belonging to the
    provider it just left and a model the new CLI has never heard of, and would
    fail at the first turn with whatever those CLIs say about a bad argument.

    So each of them is cleared back to "whatever the new provider does by
    default" rather than carried — except where the caller said what it wants in
    the same request, which wins and is validated like any other patch. Returns
    the overrides to apply; `requested` is the patch's own fields, so a caller's
    explicit choice is never silently replaced by a clear.
    """
    if new_provider not in PROVIDERS:
        raise ValidationError(
            f"Unknown provider {new_provider!r}. Known: {', '.join(PROVIDERS)}"
        )
    adapter = PROVIDERS[new_provider]
    plan: dict = {}

    if "account_id" not in requested and sess.account_id is not None:
        account = await db.get(ProviderAccount, sess.account_id)
        if account is None or account.provider != new_provider:
            plan["account_id"] = None

    if "preset_id" not in requested and sess.preset_id is not None:
        preset = await db.get(AgentPreset, sess.preset_id)
        if preset is None or preset.provider != new_provider:
            plan["preset_id"] = None

    model = requested.get("model", sess.model)
    if "model" in requested:
        # Free-text models are accepted elsewhere because a CLI can gain one
        # between releases. Not here: on a switch an unrecognised name is almost
        # always the *other* provider's model pasted through, and letting it
        # stand produces a 500-shaped failure one turn later instead of an
        # answer now.
        if model and model not in adapter.models:
            raise ValidationError(
                f"{new_provider} does not run a model called {model!r}. "
                f"It runs: {', '.join(adapter.models)}"
            )
    elif model and model not in adapter.models:
        plan["model"] = None
        model = None

    # Against the model the session is left with, not the one it had: clearing
    # the model above widens the allowed levels, and clearing the preset can
    # remove the only model there was.
    effort = requested.get("effort", sess.effort)
    if effort and "effort" not in requested and effort not in adapter.effort_choices(model):
        plan["effort"] = None

    return plan


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
    effort: str | None = None,
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
    resolved_model = model or (preset.model if preset else None)
    validate_effort(provider, resolved_model, effort)

    sess = Session(
        title=(title or "Untitled").strip()[:255] or "Untitled",
        provider=provider,
        model=resolved_model,
        effort=effort,
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
    """Persist a turn and put it in the session's queue. Commits.

    Never starts anything itself. The row lands at 'queued' and `dispatch`
    decides whether it goes now or waits behind the turn already in flight —
    which is what lets people keep typing mid-turn without two agents ever
    running against one provider session id.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValidationError("Prompt must not be empty")

    attached = await _claimable_attachments(db, session.id, attachment_ids or [])

    # Claimed here rather than read by the runner, and cleared in the same
    # commit: the briefing is owed to the *next* turn, and deciding it at queue
    # time means a failover retry rebuilds the same prompt instead of silently
    # dropping the briefing on the attempt that actually reaches the model.
    carries_handoff = bool(session.handoff_pending)
    session.handoff_pending = False

    run = Run(
        session_id=session.id,
        prompt=prompt,
        schedule_id=schedule_id,
        requested_by_id=requested_by_id,
        status="queued",
        # Stamped on the turn, so a session that later switches providers does
        # not retroactively relabel who answered this one.
        provider=session.provider,
        model=session.model or (session.preset.model if session.preset else None),
        carries_handoff=carries_handoff,
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

    # Announced before it is dispatched, so the other people in a shared session
    # see the message appear the moment it is accepted rather than only once an
    # agent picks it up — which, behind a long turn, could be minutes.
    hub.publish(
        session.id,
        {
            "type": "run.queued",
            "session_id": session.id,
            "run_id": run.id,
            "prompt": run.prompt,
            "requested_by_id": run.requested_by_id,
        },
    )
    # Awaited rather than fired off, so the decision "does this start now or
    # wait" is settled before the request returns. The row is reported as queued
    # either way; what actually happened to it arrives on the websocket.
    await runner.dispatch(session.id)
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
