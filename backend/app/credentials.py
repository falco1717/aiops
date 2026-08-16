"""Keeping a provider sign-in from lapsing while nobody is looking.

What was actually measured on the running system, because the fix only makes
sense against the real behaviour:

* the Claude CLI's OAuth access token lives **8 hours**. `expiresAt` in
  `<config_dir>/.credentials.json` is always exactly 8h after that file's mtime.
* it is refreshed **lazily, at the start of a turn**, and only when it has to
  be. A turn that began 2 seconds before the file was rewritten is the refresh;
  four later turns inside the same 8h window left the file untouched.
* `claude auth status` does *not* refresh a token that is not near expiry, so
  the sign-in status the accounts page already asks for is not keeping anything
  alive.
* a refresh **after** the access token has expired works: an 8h46m gap with no
  runs at all was followed by a turn that succeeded, having minted a new token
  from the stored refresh token.

So idle time does not, by itself, invalidate a sign-in — but it does leave the
credential stale, which means the refresh happens on the critical path of
whatever turn happens to be first, and a refresh that fails is discovered
mid-run rather than beforehand. This module moves that off the critical path:
it watches each account's expiry and, shortly before (and, if needed, just
after) it lapses, gives the CLI a reason to do its own refresh.

Two deliberate non-goals:

*   **AIOps never writes a credential file.** The obvious implementation —
    POST the refresh token to the OAuth endpoint and rewrite the JSON — would
    mean this process holding, rotating and persisting the very thing it is
    protecting, with a corrupt or half-written file as the failure mode and a
    forced re-login as the cost of getting it wrong. Letting the CLI refresh
    itself has neither failure mode: the file is only ever written by the
    process that owns it, atomically or not, and a failed attempt here leaves
    what was already on disk exactly as it was.
*   **AIOps never reads a token.** The credential file is 0600 and owned by the
    agent user, and the app deliberately cannot read it (see agent_env.py). The
    reader below runs on the agent's side of that boundary and prints back only
    an expiry timestamp, a subscription name and two booleans — never token
    material, in any branch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import agent_env
from .config import settings
from .db import SessionLocal
from .models import ProviderAccount, Run
from .redaction import redact

log = logging.getLogger("aiops.credentials")

#: Where each CLI keeps its OAuth material inside a credential directory. Only
#: Claude is handled: Codex stores a JWT whose expiry would have to be decoded
#: out of the token itself, which is exactly the thing this module refuses to
#: touch. A Codex account simply reports no expiry and is never probed.
CREDENTIAL_FILES = {"claude": ".credentials.json"}

#: Runs on the agent's side of the isolation boundary and prints one flat JSON
#: object. Every field it can emit is a bool, a number, or a short enum name —
#: there is no branch in which a token, or any prefix of one, reaches stdout.
_READER = """
import json, os, sys
path = sys.argv[1]
out = {"present": os.path.exists(path)}
try:
    with open(path, "rb") as fh:
        data = json.loads(fh.read().decode("utf-8"))
except FileNotFoundError:
    out["error"] = "no credential file"
except OSError as exc:
    out["error"] = "unreadable: %s" % exc.__class__.__name__
except ValueError:
    out["error"] = "not valid JSON"
else:
    block = data.get("claudeAiOauth") or data
    if not isinstance(block, dict):
        out["error"] = "unexpected credential shape"
    else:
        expires = block.get("expiresAt")
        out["expires_at_ms"] = expires if isinstance(expires, (int, float)) else None
        out["has_refresh_token"] = bool(block.get("refreshToken"))
        plan = block.get("subscriptionType")
        out["subscription_type"] = plan if isinstance(plan, str) else None
print(json.dumps(out))
"""


@dataclass(frozen=True)
class CredentialState:
    """What is knowable about a sign-in without reading the sign-in."""

    present: bool = False
    expires_at: datetime | None = None
    has_refresh_token: bool = False
    subscription_type: str | None = None
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.expires_at is not None


@dataclass
class Attempt:
    """What this process has already tried for one credential lifetime.

    Keyed by the expiry it was aimed at, so a credential that has since been
    refreshed — by us, by a turn, or by somebody signing in again — is a new
    lifetime and gets its own attempts. In process rather than in the database
    on purpose: AIOps is single-node (see ratelimit.py for the same call), and
    the worst a restart can cost is one extra probe.
    """

    expires_at: datetime
    before_expiry: bool = False
    after_expiry: bool = False
    last_error: str | None = None
    last_at: datetime | None = None


def should_attempt(
    expires_at: datetime | None,
    *,
    now: datetime,
    lead: timedelta,
    attempt: Attempt | None,
    retry_after: timedelta,
) -> bool:
    """Whether to give the CLI a reason to refresh, right now.

    At most twice per credential lifetime: once inside the last `lead` of it —
    when a CLI with a wide enough margin of its own will refresh — and once
    after it has actually lapsed, where a refresh is certain to be attempted.
    Two is the point: a credential whose refresh token has been revoked cannot
    be rescued by trying harder, and hammering it would turn one dead account
    into a stream of failing API calls.

    The exception is an attempt that *errored* — a timeout, a missing binary, a
    network blip — which is retried on a cooldown, because unlike a refusal it
    says nothing about whether the credential is still good.
    """
    if expires_at is None:
        return False
    if now < expires_at - lead:
        return False
    if attempt is None or attempt.expires_at != expires_at:
        return True
    past_expiry = now >= expires_at
    if past_expiry and not attempt.after_expiry:
        return True
    if not past_expiry and not attempt.before_expiry:
        return True
    if attempt.last_error and attempt.last_at is not None:
        return now - attempt.last_at >= retry_after
    return False


def describe(state: CredentialState, *, now: datetime) -> str:
    """One line for a log. Never includes anything but timing."""
    if state.error:
        return state.error
    if state.expires_at is None:
        return "no expiry recorded"
    delta = state.expires_at - now
    if delta.total_seconds() < 0:
        return f"expired {_short(-delta)} ago"
    return f"valid for another {_short(delta)}"


def _short(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


# --- reading ----------------------------------------------------------
def credential_path(account: ProviderAccount) -> str | None:
    name = CREDENTIAL_FILES.get(account.provider)
    if not name or not account.config_dir:
        return None
    return os.path.join(account.config_dir, name)


async def read_state(account: ProviderAccount) -> CredentialState:
    """Ask the agent user what it can see, and believe only the safe parts."""
    path = credential_path(account)
    if path is None:
        return CredentialState()
    try:
        proc = await agent_env.spawn(
            [sys.executable, "-c", _READER, path],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return CredentialState(error=f"could not read credential state: {type(exc).__name__}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        agent_env.kill_agent(proc)
        return CredentialState(error="timed out reading credential state")
    if proc.returncode != 0:
        detail = redact(err.decode("utf-8", "replace")).strip().splitlines()
        return CredentialState(error=(detail[-1][:120] if detail else "credential read failed"))
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        return CredentialState(error="credential reader produced no result")
    return _state_from(data)


def _state_from(data: dict) -> CredentialState:
    """Turn the reader's JSON into a state, trusting nothing about its types."""
    if not isinstance(data, dict):
        return CredentialState(error="credential reader produced no result")
    expires_at = None
    raw = data.get("expires_at_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            expires_at = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            expires_at = None
    error = data.get("error")
    plan = data.get("subscription_type")
    return CredentialState(
        present=bool(data.get("present")),
        expires_at=expires_at,
        has_refresh_token=bool(data.get("has_refresh_token")),
        subscription_type=plan if isinstance(plan, str) else None,
        error=error[:200] if isinstance(error, str) else None,
    )


# --- refreshing -------------------------------------------------------
@dataclass(frozen=True)
class RefreshOutcome:
    state: CredentialState
    refreshed: bool
    method: str | None = None
    error: str | None = None


def _account_env(account: ProviderAccount) -> dict[str, str]:
    return {**account.env(), "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb"}


async def _run(account: ProviderAccount, argv: list[str], timeout: int) -> tuple[int, str]:
    proc = await agent_env.spawn(
        argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_account_env(account),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        agent_env.kill_agent(proc)
        raise
    return proc.returncode or 0, redact(out.decode("utf-8", "replace"))


def _probe_argv() -> list[str]:
    """The smallest real turn this CLI build will do.

    A turn is what actually makes the CLI refresh, so the probe is one — with
    no tools, no MCP servers and no session written to disk, on the cheapest
    model. Measured on 2.1.232: ~2s and a fifth of a cent.
    """
    return [
        settings.claude_bin,
        "-p",
        "ok",
        "--model",
        settings.credential_probe_model,
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--session-id",
        str(uuid.uuid4()),
    ]


async def attempt_refresh(account: ProviderAccount) -> RefreshOutcome:
    """Give the CLI an opportunity to refresh, cheapest first.

    Nothing here forces anything: the CLI decides whether its token needs
    replacing, and both steps are no-ops against a credential it is happy with.
    """
    before = await read_state(account)
    if before.error and not before.present:
        return RefreshOutcome(before, False, error=before.error)

    for method, argv, timeout in (
        # Free, and enough on a build whose refresh margin is wide.
        ("auth status", [settings.claude_bin, "auth", "status", "--json"], 30),
        ("probe turn", _probe_argv(), settings.credential_probe_timeout_seconds),
    ):
        if method == "probe turn" and not settings.credential_probe_enabled:
            break
        try:
            code, output = await _run(account, argv, timeout)
        except FileNotFoundError:
            return RefreshOutcome(before, False, error=f"{argv[0]} is not installed")
        except asyncio.TimeoutError:
            return RefreshOutcome(before, False, method=method, error=f"{method} timed out")
        except OSError as exc:
            return RefreshOutcome(before, False, method=method, error=f"{method}: {exc}")

        after = await read_state(account)
        if _advanced(before, after):
            return RefreshOutcome(after, True, method=method)
        if code != 0:
            tail = [line for line in output.strip().splitlines() if line.strip()]
            return RefreshOutcome(
                after,
                False,
                method=method,
                error=(tail[-1][:200] if tail else f"{method} exited {code}"),
            )

    # Ran cleanly and the CLI kept the credential it had: it is still good, and
    # this was simply earlier than the CLI's own refresh margin.
    return RefreshOutcome(await read_state(account), False)


def _advanced(before: CredentialState, after: CredentialState) -> bool:
    if after.expires_at is None:
        return False
    return before.expires_at is None or after.expires_at > before.expires_at


# --- the loop ---------------------------------------------------------
_attempts: dict[int, Attempt] = {}


def _reset_attempts() -> None:
    """Test seam; the loop itself has no reason to forget."""
    _attempts.clear()


async def _busy(db, account: ProviderAccount) -> bool:
    """True while a turn is running on this account.

    A turn refreshes on its own, and two CLI processes racing to rewrite one
    credential file is precisely the corruption this is supposed to prevent.
    Whatever is due can wait a tick.
    """
    return bool(
        await db.scalar(
            select(Run.id)
            .where(Run.account_id == account.id, Run.status.in_(("queued", "running")))
            .limit(1)
        )
    )


async def refresh_due_accounts(now: datetime | None = None) -> int:
    """One pass. Returns how many accounts were actually refreshed."""
    now = now or datetime.now(timezone.utc)
    lead = timedelta(seconds=settings.credential_refresh_lead_seconds)
    retry_after = timedelta(seconds=settings.credential_retry_seconds)
    refreshed = 0

    async with SessionLocal() as db:
        accounts = list(
            await db.scalars(
                select(ProviderAccount).where(
                    ProviderAccount.provider.in_(tuple(CREDENTIAL_FILES))
                )
            )
        )
        live = {a.id for a in accounts}
        for stale in [key for key in _attempts if key not in live]:
            del _attempts[stale]

        for account in accounts:
            state = await read_state(account)
            account.credential_checked_at = now
            account.credential_expires_at = state.expires_at
            if not state.known:
                # Not signed in, or a shape we do not recognise. Either way
                # there is nothing to keep alive and nothing to warn about.
                _attempts.pop(account.id, None)
                continue

            attempt = _attempts.get(account.id)
            if not should_attempt(
                state.expires_at, now=now, lead=lead, attempt=attempt, retry_after=retry_after
            ):
                continue
            if await _busy(db, account):
                log.debug("%s has a turn in flight; leaving its refresh to that", account.name)
                continue

            log.info("refreshing %s credential (%s)", account.name, describe(state, now=now))
            outcome = await attempt_refresh(account)

            if attempt is None or attempt.expires_at != state.expires_at:
                attempt = Attempt(expires_at=state.expires_at)
                _attempts[account.id] = attempt
            if now >= state.expires_at:
                attempt.after_expiry = True
            else:
                attempt.before_expiry = True
            attempt.last_at = now
            attempt.last_error = outcome.error

            account.credential_expires_at = outcome.state.expires_at
            account.credential_refresh_error = outcome.error[:200] if outcome.error else None
            if outcome.refreshed:
                refreshed += 1
                account.credential_refreshed_at = now
                # A successful refresh is a new lifetime; drop the bookkeeping
                # for the old one so the new one starts with a clean slate.
                _attempts.pop(account.id, None)
                log.info(
                    "%s credential refreshed via %s (%s)",
                    account.name,
                    outcome.method,
                    describe(outcome.state, now=now),
                )
            elif outcome.error:
                log.warning("%s credential refresh failed: %s", account.name, outcome.error)
            else:
                log.info(
                    "%s credential left as it was — the CLI does not consider it due yet (%s)",
                    account.name,
                    describe(outcome.state, now=now),
                )
        await db.commit()
    return refreshed


async def credential_loop() -> None:
    """Poll every account's credential expiry. One tick per check interval."""
    log.info(
        "credential watch started (every %ss, refreshing within %ss of expiry)",
        settings.credential_check_seconds,
        settings.credential_refresh_lead_seconds,
    )
    while True:
        try:
            await refresh_due_accounts()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad account must not stop the watch
            log.exception("credential watch tick failed")
        await asyncio.sleep(settings.credential_check_seconds)


def state_for_output(account: ProviderAccount, state: CredentialState) -> CredentialState:
    """Prefer a live read, fall back to what the watch last recorded."""
    if state.known or state.error:
        return state
    return replace(state, expires_at=account.credential_expires_at)
