from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .events import hub
from .models import Event, ProviderAccount, Run, Session
from .providers import get_provider

log = logging.getLogger("aiops.runner")

# Agent stdout lines can be large (a tool result carrying a whole file), well
# past asyncio's default 64 KiB stream limit.
STREAM_LIMIT = 16 * 1024 * 1024

# SIGKILL is POSIX-only; on Windows fall back to the terminate signal.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


class Runner:
    """Owns the lifecycle of every agent subprocess."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        # Cancellation is recorded, not inferred: a signalled process reports
        # -SIGTERM on POSIX, 1 on Windows, and 143 only when the agent traps the
        # signal itself, so the exit code cannot tell us what the operator meant.
        self._cancelled: set[int] = set()

    # -- public API ----------------------------------------------------
    def submit(self, run_id: int) -> None:
        task = asyncio.create_task(self._guard(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    async def cancel(self, run_id: int) -> bool:
        self._cancelled.add(run_id)
        proc = self._procs.get(run_id)
        if proc is None:
            task = self._tasks.get(run_id)
            if task:
                task.cancel()
                return True
            self._cancelled.discard(run_id)
            return False
        _terminate(proc)
        return True

    async def shutdown(self) -> None:
        """Best-effort teardown; nothing here may prevent the process from exiting."""
        for run_id in list(self._procs):
            try:
                await self.cancel(run_id)
            except Exception:  # noqa: BLE001
                log.warning("failed to cancel run %s during shutdown", run_id, exc_info=True)
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=15)

    # -- internals -----------------------------------------------------
    async def _guard(self, run_id: int) -> None:
        try:
            async with self._sem:
                await self._execute(run_id)
        except asyncio.CancelledError:
            # Awaiting inside a cancelled task re-raises immediately, so shield the
            # bookkeeping — otherwise the row would be left stuck at "running".
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(
                    self._finalize(run_id, "cancelled", None, "Cancelled before completion")
                )
            raise
        except Exception as exc:  # noqa: BLE001 - a crash here must not kill the server
            log.exception("run %s failed", run_id)
            await self._finalize(run_id, "failed", None, f"{type(exc).__name__}: {exc}")
        finally:
            self._cancelled.discard(run_id)

    async def _execute(self, run_id: int) -> None:
        """Run one turn, failing over to another account if a limit is hit."""
        attempted: list[int] = []
        previous: ProviderAccount | None = None

        while True:
            outcome = await self._attempt(run_id, attempted, previous)
            if outcome is None:
                return  # already finalized
            status, exit_code, state, account, next_account = outcome
            if status == "rate_limited" and next_account is not None:
                log.info(
                    "run %s: %s is limited, failing over to %s",
                    run_id,
                    account.name if account else "default",
                    next_account.name,
                )
                attempted.append(next_account.id)
                previous = account
                continue
            if status == "rate_limited":
                status = "failed"
                # Keep the provider's own wording, but say what to do about it —
                # the CLI's message alone doesn't mention that failover exists.
                guidance = (
                    "No fallback account is configured for this one. Set one on the "
                    "Accounts page to have AIOps switch automatically, or wait for the "
                    "limit to reset."
                )
                state.error = f"{state.error}\n\n{guidance}" if state.error else guidance
            await self._finalize(
                run_id,
                status,
                exit_code,
                state.error,
                state.cost_usd,
                usage=state.usage,
                account_id=account.id if account else None,
                failed_over_from_id=previous.id if previous else None,
            )
            return

    async def _attempt(
        self,
        run_id: int,
        attempted: list[int],
        previous: "ProviderAccount | None",
    ):
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if run is None or run.status not in ("queued", "running"):
                return None
            sess = await db.get(Session, run.session_id)
            if sess is None:
                await self._finalize(run_id, "failed", None, "Session no longer exists")
                return None

            provider = get_provider(sess.provider)
            preset = sess.preset
            workspace = sess.workspace
            cwd = workspace.path if workspace else settings.workspace_root
            if not os.path.isdir(cwd):
                await self._finalize(run_id, "failed", None, f"Workspace directory missing: {cwd}")
                return None

            account = await self._pick_account(db, sess, attempted)
            next_account = await self._next_account(db, account, attempted)
            account_env: dict[str, str] = {}
            if account is not None:
                os.makedirs(account.config_dir, exist_ok=True)
                account_env = account.env()

            spec = provider.build_run(
                prompt=run.prompt,
                model=sess.model or (preset.model if preset else None),
                provider_session_id=sess.provider_session_id,
                permission_mode=preset.permission_mode if preset else None,
                system_prompt=preset.system_prompt if preset else None,
                allowed_tools=preset.allowed_tools if preset else None,
                extra_args=(preset.extra_args if preset else []) or [],
                stream_partials=settings.stream_partial_messages,
                account_env=account_env,
            )

            if spec.assigned_session_id and not sess.provider_session_id:
                sess.provider_session_id = spec.assigned_session_id

            run.status = "running"
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.command = spec.argv
            run.account_id = account.id if account else None
            sess.status = "running"
            await db.commit()

            hub.publish(
                sess.id,
                {
                    "type": "run.started",
                    "session_id": sess.id,
                    "run_id": run.id,
                    "prompt": run.prompt,
                    "command": _redact(spec.argv),
                    "account": account.name if account else None,
                    "failed_over_from": previous.name if previous else None,
                },
            )

            env = {**os.environ, **spec.env, "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb"}
            try:
                proc = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=STREAM_LIMIT,
                    start_new_session=True,
                )
            except FileNotFoundError:
                await self._finalize(
                    run_id,
                    "failed",
                    None,
                    f"Executable not found: {spec.argv[0]}. Is the CLI installed in the container?",
                )
                return

            self._procs[run_id] = proc
            state = _RunState()
            try:
                stderr_task = asyncio.create_task(_drain(proc.stderr, state))
                await asyncio.wait_for(
                    self._pump_stdout(db, run, sess, provider, proc, state),
                    timeout=settings.run_timeout_seconds,
                )
                exit_code = await asyncio.wait_for(proc.wait(), timeout=30)
                await stderr_task
                status = self._classify(run_id, exit_code, state)
                if status == "failed" and state.rate_limited:
                    status = "rate_limited"
                    if account is not None:
                        # Skip this account for a while rather than re-picking it
                        # on the operator's next turn.
                        account.limited_until = datetime.now(timezone.utc) + timedelta(
                            seconds=settings.account_limit_cooldown_seconds
                        )
                        await db.commit()
            except asyncio.TimeoutError:
                _terminate(proc)
                exit_code = await _wait_quietly(proc)
                if run_id in self._cancelled:
                    status = "cancelled"
                    state.error = "Cancelled by operator"
                else:
                    status = "timeout"
                    state.error = state.error or (
                        f"Run exceeded {settings.run_timeout_seconds}s and was terminated"
                    )
            finally:
                self._procs.pop(run_id, None)
                self._cancelled.discard(run_id)

        return status, exit_code, state, account, next_account

    # -- account selection ---------------------------------------------
    @staticmethod
    async def _pick_account(db, sess: Session, attempted: list[int]):
        """The account this attempt should use.

        Returns None when no accounts are configured at all, which runs the CLI
        against its ambient credentials — the behaviour before named accounts.
        """
        from .models import ProviderAccount

        if attempted:
            return await db.get(ProviderAccount, attempted[-1])
        if sess.account_id:
            chosen = await db.get(ProviderAccount, sess.account_id)
            if chosen is not None:
                return chosen
        now = datetime.now(timezone.utc)
        rows = list(
            await db.scalars(
                select(ProviderAccount)
                .where(ProviderAccount.provider == sess.provider)
                .order_by(ProviderAccount.is_default.desc(), ProviderAccount.id)
            )
        )
        healthy = [a for a in rows if _aware(a.limited_until) is None or _aware(a.limited_until) <= now]
        return (healthy or rows or [None])[0]

    @staticmethod
    async def _next_account(db, account, attempted: list[int]):
        """The configured fallback, if it hasn't already been tried."""
        from .models import ProviderAccount

        if account is None or account.fallback_account_id is None:
            return None
        if account.fallback_account_id in attempted or account.fallback_account_id == account.id:
            return None
        candidate = await db.get(ProviderAccount, account.fallback_account_id)
        if candidate is None:
            return None
        limited = _aware(candidate.limited_until)
        if limited and limited > datetime.now(timezone.utc):
            return None
        return candidate

    async def _pump_stdout(self, db, run: Run, sess: Session, provider, proc, state) -> None:
        # Continue the run's existing sequence: a failover attempt writes more
        # events for the same run, and restarting at 1 collides with the events
        # the first attempt already stored.
        seq = (
            await db.scalar(select(func.max(Event.seq)).where(Event.run_id == run.id))
        ) or 0
        while True:
            try:
                raw_line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # Single line beyond STREAM_LIMIT — skip it rather than abort the run.
                log.warning("run %s: dropped an oversized output line", run.id)
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            event = provider.parse_line(line)
            if event is None:
                continue

            if event.provider_session_id and event.provider_session_id != sess.provider_session_id:
                sess.provider_session_id = event.provider_session_id
                await db.commit()
            if event.cost_usd is not None:
                state.cost_usd = (state.cost_usd or 0) + event.cost_usd
            if event.usage:
                for key, value in event.usage.items():
                    state.usage[key] = state.usage.get(key, 0) + value
            if event.rate_limited:
                state.rate_limited = True
            if event.is_error and event.kind in ("result", "error"):
                state.saw_error = True
                state.error = state.error or event.text

            payload = {
                "type": "event",
                "session_id": sess.id,
                "run_id": run.id,
                "kind": event.kind,
                "text": event.text,
                "tool_name": event.tool_name,
                "is_error": event.is_error,
                "parent_tool_use_id": event.parent_tool_use_id,
                "agent_name": event.agent_name,
            }

            if event.persist:
                seq += 1
                db.add(
                    Event(
                        run_id=run.id,
                        session_id=sess.id,
                        seq=seq,
                        kind=event.kind,
                        text=event.text,
                        tool_name=event.tool_name,
                        raw=event.raw,
                        parent_tool_use_id=event.parent_tool_use_id,
                        agent_name=event.agent_name,
                    )
                )
                await db.commit()
                payload["seq"] = seq

            hub.publish(sess.id, payload)

    def _classify(self, run_id: int, exit_code: int | None, state: "_RunState") -> str:
        if run_id in self._cancelled:
            state.error = "Cancelled by operator"
            return "cancelled"
        if exit_code == 0 and not state.saw_error:
            return "succeeded"
        return "failed"

    async def _finalize(
        self,
        run_id: int,
        status: str,
        exit_code: int | None,
        error: str | None,
        cost_usd: float | None = None,
        usage: dict[str, int] | None = None,
        account_id: int | None = None,
        failed_over_from_id: int | None = None,
    ) -> None:
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.exit_code = exit_code
            run.finished_at = datetime.now(timezone.utc)
            # stderr is collected for diagnostics, but the CLIs also write
            # harmless notices there. Recording it on a successful run makes a
            # working turn look broken.
            if error and status != "succeeded":
                run.error = error[:8000]
            if cost_usd is not None:
                run.cost_usd = cost_usd
            if account_id is not None:
                run.account_id = account_id
            if failed_over_from_id is not None:
                run.failed_over_from_id = failed_over_from_id
            if usage:
                run.input_tokens = usage.get("input_tokens")
                run.output_tokens = usage.get("output_tokens")
                run.cache_read_tokens = usage.get("cache_read_tokens")
                run.cache_write_tokens = usage.get("cache_write_tokens")
                # What the model had to read this turn — the practical measure
                # of context pressure.
                run.context_tokens = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_read_tokens") or 0)
                    + (usage.get("cache_write_tokens") or 0)
                )
            sess = await db.get(Session, run.session_id)
            if sess is not None:
                still_busy = await db.scalar(
                    select(Run.id)
                    .where(Run.session_id == sess.id, Run.status.in_(("queued", "running")))
                    .limit(1)
                )
                sess.status = "running" if still_busy else ("error" if status == "failed" else "idle")
                sess.updated_at = datetime.now(timezone.utc)
            await db.commit()
            session_id = run.session_id

        hub.publish(
            session_id,
            {
                "type": "run.finished",
                "session_id": session_id,
                "run_id": run_id,
                "status": status,
                "exit_code": exit_code,
                "error": error,
                "cost_usd": cost_usd,
            },
        )


class _RunState:
    def __init__(self) -> None:
        self.error: str | None = None
        self.cost_usd: float | None = None
        self.saw_error = False
        self.rate_limited = False
        self.usage: dict[str, int] = {}


async def _drain(stream, state: _RunState) -> None:
    """Collect stderr; the CLIs use it for warnings and fatal startup errors."""
    chunks: list[str] = []
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            chunks.append(text)
    if chunks and not state.error:
        state.error = "\n".join(chunks[-40:])


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the agent's whole process group, falling back to the process alone.

    Agents spawn children (test runners, dev servers, package installs); killing
    only the agent would orphan them. `start_new_session=True` puts each run in
    its own group so one signal reaches all of it. Windows has no process
    groups, so dev machines take the single-process path.
    """
    if proc.returncode is not None:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError, OSError):
        if sig == _SIGKILL and _SIGKILL != signal.SIGTERM:
            proc.kill()
        else:
            proc.terminate()


def _terminate(proc: asyncio.subprocess.Process) -> None:
    _signal_group(proc, signal.SIGTERM)


async def _wait_quietly(proc: asyncio.subprocess.Process) -> int | None:
    try:
        return await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        _signal_group(proc, _SIGKILL)
        return await proc.wait()


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres returns aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _redact(argv: list[str]) -> list[str]:
    """The prompt can be long; show the shape of the command, not the payload."""
    out = []
    for arg in argv:
        out.append(arg if len(arg) <= 120 else arg[:117] + "...")
    return out


runner = Runner()
