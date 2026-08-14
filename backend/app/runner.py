from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import agent_env, exposure, handoff
from .approvals import broker, run_tokens
from .config import settings
from .db import SessionLocal
from .events import hub
from .models import Attachment, Event, ProviderAccount, Run, Session, User
from . import attachments, relay, ssh_targets
from .providers import get_provider
from .providers.base import NormalizedEvent
from .providers.codex_appserver import CodexAppServerAdapter

log = logging.getLogger("aiops.runner")

# Agent stdout lines can be large (a tool result carrying a whole file), well
# past asyncio's default 64 KiB stream limit.
STREAM_LIMIT = 16 * 1024 * 1024

# SIGKILL is POSIX-only; on Windows fall back to the terminate signal.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

#: How bubblewrap reports that the kernel or the container's seccomp profile
#: will not let it build a sandbox. Codex's own error text is unhelpful about
#: what an operator can actually change, so we say it ourselves.
_BWRAP_ERROR = re.compile(r"bwrap:[^\n]*(namespace|permission|seccomp)", re.IGNORECASE)

BWRAP_HINT = (
    "This command was approved but bubblewrap could not build its sandbox, so "
    "it never ran. Codex's sandbox tiers below 'danger-full-access' need an "
    "unprivileged user namespace and a mount, and Docker's default seccomp and "
    "AppArmor profiles each block one of those. Either run this container with "
    "both `seccomp=unconfined` and `apparmor=unconfined` (neither alone is "
    "enough), or set AIOPS_CODEX_INTERACTIVE_SANDBOX=danger-full-access to run "
    "approved commands unsandboxed — in that mode the human approval is the "
    "only control left."
)


class Runner:
    """Owns the lifecycle of every agent turn.

    A turn runs one of two ways. Most of them are a one-shot CLI subprocess
    streaming NDJSON on stdout. The exception is Codex in "ask" mode: `codex
    exec` has no way to stop and put a question to a human, so that turn is a
    JSON-RPC conversation with `codex app-server` held open by this process
    instead (see CodexAppServerAdapter). The two paths differ only in how bytes
    arrive — everything after an event is parsed is shared, in _EventSink.
    """

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        # An interactive Codex turn is not a one-shot subprocess we can signal:
        # it is a JSON-RPC conversation this process is holding open, so it is
        # tracked separately and stopped by closing the adapter.
        self._adapters: dict[int, CodexAppServerAdapter] = {}
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
        if proc is not None:
            _terminate(proc)
            return True
        adapter = self._adapters.get(run_id)
        if adapter is not None:
            # Release anything parked on a human first: the adapter's approval
            # callback is awaiting the broker inside this same process, and
            # closing the transport underneath it would not wake it up.
            await broker.cancel_run(run_id)
            await adapter.close()
            return True
        task = self._tasks.get(run_id)
        if task:
            task.cancel()
            return True
        self._cancelled.discard(run_id)
        return False

    async def shutdown(self) -> None:
        """Best-effort teardown; nothing here may prevent the process from exiting."""
        for run_id in [*self._procs, *self._adapters]:
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
        # Tokens and cost from an abandoned attempt were still spent, so they
        # carry forward rather than being lost when a limit forces a retry.
        carried: dict[str, int] = {}
        carried_cost = 0.0

        while True:
            outcome = await self._attempt(run_id, attempted, previous)
            if outcome is None:
                return  # already finalized
            status, exit_code, state, account, next_account = outcome
            for key, value in state.usage.items():
                state.usage[key] = value + carried.get(key, 0)
            state.cost_usd = (state.cost_usd or 0) + carried_cost
            if status == "rate_limited" and next_account is not None:
                carried = dict(state.usage)
                carried_cost = state.cost_usd or 0.0
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
                # The agent writes its own credentials here, so the directory
                # itself has to be reachable by it. Only the directory: what is
                # already inside was shared at startup, and walking a CLI's
                # whole state directory on every turn is not free.
                agent_env.grant_agent_access(
                    account.config_dir, writable=True, recursive=False
                )
                account_env = account.env()

            approval_mode = sess.approval_mode or settings.default_approval_mode
            # A scheduled run has nobody watching, so parking it on a question
            # would just burn the approval timeout and fail. Unattended work
            # runs at the session's non-interactive equivalent instead.
            if approval_mode == "ask" and run.schedule_id is not None:
                approval_mode = "auto"

            # `codex exec` cannot ask a human anything, so an interactive Codex
            # turn is driven over the app-server's JSON-RPC protocol instead of
            # as a one-shot subprocess. Every other combination — Codex on auto
            # or bypass, and Claude in all three modes — keeps the CLI path.
            interactive_codex = (
                sess.provider == "codex"
                and approval_mode == "ask"
                and provider.supports_interactive_approval
            )
            # Only the CLI path needs a token: it identifies a bridge running as
            # a grandchild process. The adapter is in-process and calls the
            # broker directly, so issuing one for it would be a secret with no
            # holder.
            token = (
                run_tokens.issue(run.id, sess.id)
                if approval_mode == "ask"
                and provider.supports_interactive_approval
                and not interactive_codex
                else None
            )

            # Systems the operator has stored. Their credentials are written
            # into a private per-run directory and removed in the finally
            # below, so `ssh <name>` works for this turn and nothing is left
            # on disk afterwards.
            # Scoped to whoever owns the session: stored credentials belong to
            # the person who saved them, so a turn only reaches systems that
            # person may reach.
            # Whoever asked for this turn, not whoever owns the session: a
            # shared session must not lend its owner's stored credentials to
            # everyone able to type into it.
            asker = (
                await db.get(User, run.requested_by_id) if run.requested_by_id else None
            )
            targets = await ssh_targets.visible_targets(db, asker)
            # Systems bound to a relay node are reached through it. The nodes
            # are looked up here so the generated config can name them, and so
            # this run's permission to use one covers exactly these hosts.
            nodes = await relay.nodes_for_targets(db, targets)
            ssh_ctx = ssh_targets.prepare(targets, nodes)
            # A decrypted private key is on disk from here until cleanup, so
            # everything that follows runs inside this try. It used to be a
            # `finally` that opened ninety lines further down, which left the
            # key behind for every path that never reached it: an operator
            # cancelling the run, a shutdown cancelling all of them, or the
            # commit below failing on a row somebody deleted underneath it.
            try:
                usable = [t for t in targets if ssh_ctx and t.slug in ssh_ctx.names]
                target_note = ssh_targets.describe(usable, nodes)

                # Say in the transcript that somebody's stored credentials were
                # put to work in front of other people. Written here, next to the
                # decision, rather than at the API — the API knows what was
                # asked for, this knows what the turn actually got. Silent when
                # the session has no other readers, which is most of them.
                exposure_note = await exposure.record_use(
                    db, run, sess, usable, asker=asker
                )
                preset_prompt = preset.system_prompt if preset else None
                system_prompt = "\n\n".join(p for p in (preset_prompt, target_note) if p) or None

                # The transcript keeps the operator's own words; the agent gets them
                # plus where the files landed. Storing the paths on the run instead
                # would put a block of container paths in every message bubble.
                attached = list(
                    await db.scalars(
                        select(Attachment)
                        .where(Attachment.run_id == run.id)
                        .order_by(Attachment.created_at)
                    )
                )
                agent_prompt = run.prompt + attachments.prompt_suffix(attached)

                # The first turn after a provider switch is talking to an agent
                # that has never seen this conversation: its CLI cannot load the
                # other's session, so the only continuity available is a summary
                # AIOps writes from its own transcript. It goes in front of the
                # operator's words and nowhere near `run.prompt`, exactly as the
                # attachment paths above go after them — the transcript shows
                # what the operator actually typed.
                if run.carries_handoff:
                    briefing = await handoff.build_digest(db, sess, before_run_id=run.id)
                    if briefing:
                        agent_prompt = f"{briefing}\n\n{agent_prompt}"

                adapter: CodexAppServerAdapter | None = None
                if interactive_codex:
                    # A preset that pins a tier wins; otherwise the instance-wide
                    # setting decides, because which tiers actually work depends on
                    # what the container is allowed to do (see BWRAP_HINT).
                    sandbox = (
                        preset.permission_mode if preset else None
                    ) or settings.codex_interactive_sandbox
                    adapter = CodexAppServerAdapter(
                        prompt=agent_prompt,
                        cwd=cwd,
                        model=sess.model or (preset.model if preset else None),
                        effort=sess.effective_effort,
                        sandbox=sandbox,
                        resume_id=sess.provider_session_id,
                        system_prompt=system_prompt,
                        on_approval=self._approval_callback(run.id, sess.id),
                        env={
                            "NO_COLOR": "1",
                            "FORCE_COLOR": "0",
                            "TERM": "dumb",
                            **account_env,
                            **(ssh_ctx.env if ssh_ctx else {}),
                        },
                        stream_partials=settings.stream_partial_messages,
                    )
                    # No argv is ever exec'd with a prompt on it here, so `command`
                    # shows what really launches plus the tier the turn runs under —
                    # the two things an operator needs when a command dies.
                    argv = [
                        settings.codex_bin,
                        "app-server",
                        f"(sandbox={sandbox}; approvals=untrusted)",
                    ]
                else:
                    spec = provider.build_run(
                        prompt=agent_prompt,
                        model=sess.model or (preset.model if preset else None),
                        effort=sess.effective_effort,
                        provider_session_id=sess.provider_session_id,
                        permission_mode=preset.permission_mode if preset else None,
                        system_prompt=system_prompt,
                        allowed_tools=preset.allowed_tools if preset else None,
                        extra_args=(preset.extra_args if preset else []) or [],
                        stream_partials=settings.stream_partial_messages,
                        account_env=account_env,
                        approval_mode=approval_mode,
                        approval_token=token,
                    )
                    argv = spec.argv
                    if spec.assigned_session_id and not sess.provider_session_id:
                        sess.provider_session_id = spec.assigned_session_id

                run.status = "running"
                run.started_at = run.started_at or datetime.now(timezone.utc)
                run.command = argv
                # Confirmed against what is actually about to run: the row was
                # stamped when the turn was queued, and the session's model can
                # be re-pointed while it waits. The provider cannot — a switch is
                # refused while a turn is outstanding.
                run.provider = sess.provider
                run.model = sess.model or (preset.model if preset else None)
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
                        "command": _redact(argv),
                        "account": account.name if account else None,
                        "failed_over_from": previous.name if previous else None,
                        # A retry after failover is a continuation, not a new turn;
                        # the UI must not clear what the first attempt streamed.
                        "attempt": len(attempted) + 1,
                    },
                )

                # After run.started, so it lands under the turn it belongs to
                # rather than ahead of it. Committed above with the run row.
                if exposure_note is not None:
                    hub.publish(
                        sess.id,
                        {
                            "type": "event",
                            "session_id": sess.id,
                            "run_id": run.id,
                            "seq": exposure_note.seq,
                            "kind": exposure_note.kind,
                            "text": exposure_note.text,
                            "tool_name": None,
                            "is_error": False,
                            "parent_tool_use_id": None,
                            "agent_name": None,
                        },
                    )

                state = _RunState()
                try:
                    if adapter is not None:
                        status, exit_code = await self._drive_adapter(
                            db, run, sess, adapter, state, account
                        )
                    else:
                        env = {
                            **spec.env,
                            "NO_COLOR": "1",
                            "FORCE_COLOR": "0",
                            "TERM": "dumb",
                        }
                        if ssh_ctx:
                            env.update(ssh_ctx.env)
                        try:
                            proc = await agent_env.spawn(
                                spec.argv,
                                cwd=cwd,
                                env=env,
                                stdin=asyncio.subprocess.DEVNULL,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                limit=STREAM_LIMIT,
                            )
                        except FileNotFoundError:
                            await self._finalize(
                                run_id,
                                "failed",
                                None,
                                f"Executable not found: {spec.argv[0]}. "
                                "Is the CLI installed in the container?",
                            )
                            return
                        status, exit_code = await self._drive_process(
                            db, run, sess, provider, proc, state, account
                        )
                finally:
                    self._cancelled.discard(run_id)
                    # Nothing may stay parked on a run that is ending: the bridge
                    # dies with the agent, so a still-pending request would leave
                    # buttons in the UI answering a process nobody can reach.
                    run_tokens.revoke(run_id)
                    await broker.cancel_run(run_id)

            finally:
                # Stored credentials exist on disk only for the life of the
                # attempt that wrote them — failover makes a fresh set.
                if ssh_ctx:
                    ssh_ctx.cleanup()
        return status, exit_code, state, account, next_account

    # -- execution engines ---------------------------------------------
    async def _drive_process(self, db, run: Run, sess: Session, provider, proc, state, account):
        """Supervise one agent CLI subprocess, streaming its stdout."""
        run_id = run.id
        self._procs[run_id] = proc
        stderr_task = asyncio.create_task(_drain(proc.stderr, state))
        try:
            await asyncio.wait_for(
                self._pump_stdout(db, run, sess, provider, proc, state, account),
                timeout=settings.run_timeout_seconds,
            )
            exit_code = await asyncio.wait_for(proc.wait(), timeout=30)
            await stderr_task
            status = self._classify(run_id, exit_code, state)
            if status == "failed" and state.rate_limited:
                status = "rate_limited"
                await self._cool_down(db, account)
        except asyncio.TimeoutError:
            _terminate(proc)
            exit_code = await _wait_quietly(proc)
            status, state.error = self._interrupted(run_id, state)
        finally:
            # Any exit from here — a DB error in the pump, a cancellation
            # landing before the process was registered, an unexpected
            # crash — must still reap the agent. Dropping it from _procs
            # without signalling it leaves a subprocess running that
            # nothing can reach any more.
            if proc.returncode is None:
                _terminate(proc)
                await _wait_quietly(proc)
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            self._procs.pop(run_id, None)
        return status, exit_code

    async def _drive_adapter(
        self, db, run: Run, sess: Session, adapter: CodexAppServerAdapter, state, account
    ):
        """Supervise one interactive Codex turn over the app-server protocol."""
        run_id = run.id
        self._adapters[run_id] = adapter
        try:
            await asyncio.wait_for(
                self._pump_adapter(db, run, sess, adapter, state, account),
                timeout=settings.run_timeout_seconds,
            )
            # There is no process exit code to read: the app-server is a
            # conversation, not a one-shot command, and it is still healthy when
            # the turn ends. What the turn reported is the whole verdict.
            exit_code = 1 if state.saw_error else 0
            status = self._classify(run_id, exit_code, state)
            if status == "failed" and state.rate_limited:
                status = "rate_limited"
                await self._cool_down(db, account)
        except asyncio.TimeoutError:
            exit_code = None
            status, state.error = self._interrupted(run_id, state)
        finally:
            await adapter.close()
            self._adapters.pop(run_id, None)

        # The thread id is what lets the operator's next turn continue this
        # conversation instead of starting a fresh one.
        if adapter.conversation_id and adapter.conversation_id != sess.provider_session_id:
            sess.provider_session_id = adapter.conversation_id
            await db.commit()
        # `turn/completed` carries the usage that the sink has already folded
        # in. Only when the turn never got that far — a denial that ended it, a
        # dead server — does the adapter's own last-seen tally add anything.
        if adapter.usage and not state.usage:
            state.usage.update(adapter.usage)
        return status, exit_code

    def _approval_callback(self, run_id: int, session_id: str):
        """Ask the operator, straight from the adapter's request handler.

        Claude has to reach the broker through an MCP bridge subprocess holding
        a run token, because the CLI owns the tool loop. The Codex adapter runs
        inside this process, so it just calls the broker — no bridge, no token,
        no HTTP hop that could fail open.
        """

        async def ask(kind, tool_name, summary, request):
            decision = await broker.request(
                run_id=run_id,
                session_id=session_id,
                provider="codex",
                kind=kind,
                tool_name=tool_name,
                summary=summary,
                request=request if isinstance(request, dict) else None,
            )
            return decision.allowed, decision.note

        return ask

    def _interrupted(self, run_id: int, state: "_RunState") -> tuple[str, str | None]:
        """What a run that hit the wall clock should be recorded as."""
        if run_id in self._cancelled:
            return "cancelled", "Cancelled by operator"
        return "timeout", state.error or (
            f"Run exceeded {settings.run_timeout_seconds}s and was terminated"
        )

    @staticmethod
    async def _cool_down(db, account) -> None:
        """Skip a limited account for a while rather than re-picking it."""
        if account is None:
            return
        account.limited_until = datetime.now(timezone.utc) + timedelta(
            seconds=settings.account_limit_cooldown_seconds
        )
        await db.commit()

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

    async def _pump_stdout(
        self, db, run: Run, sess: Session, provider, proc, state, account=None
    ) -> None:
        sink = await _EventSink.open(db, run, sess, state, account)
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
            await sink.emit(event)

    async def _pump_adapter(
        self, db, run: Run, sess: Session, adapter: CodexAppServerAdapter, state, account=None
    ) -> None:
        """Same treatment for events that arrive as objects rather than lines."""
        sink = await _EventSink.open(db, run, sess, state, account)
        async for event in adapter.run():
            await sink.emit(event)

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


class _EventSink:
    """Applies one parsed event to the run in progress.

    Everything an event can carry beyond its text — a session id to remember,
    tokens and cost to accumulate, a rate-limit window, the slash commands the
    CLI advertised, the database row, the websocket fan-out — is handled here
    and only here. Both execution paths (an agent CLI's stdout and the Codex
    app-server adapter) push through this same object, so the interactive path
    cannot quietly drift away from the one the CLIs use.
    """

    def __init__(self, db, run: Run, sess: Session, state: "_RunState", account, seq: int) -> None:
        self.db = db
        self.run = run
        self.sess = sess
        self.state = state
        self.account = account
        self.seq = seq
        # Only the tool call that spawns a subagent carries its name; the
        # child's own messages point back at it by id.
        self.spawn_names: dict[str, str] = {}
        self._warned_bwrap = False

    @classmethod
    async def open(cls, db, run: Run, sess: Session, state: "_RunState", account) -> "_EventSink":
        # Continue the run's existing sequence: a failover attempt writes more
        # events for the same run, and restarting at 1 collides with the events
        # the first attempt already stored.
        seq = (await db.scalar(select(func.max(Event.seq)).where(Event.run_id == run.id))) or 0
        return cls(db, run, sess, state, account, seq)

    async def emit(self, event: NormalizedEvent) -> None:
        db, run, sess, state = self.db, self.run, self.sess, self.state

        # Label a subagent's steps with the name it was spawned as; only the
        # spawning tool call carries it, the child messages do not.
        if event.spawns_tool_use_id and event.agent_name:
            self.spawn_names[event.spawns_tool_use_id] = event.agent_name
        if event.parent_tool_use_id and not event.agent_name:
            event.agent_name = self.spawn_names.get(event.parent_tool_use_id)

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
        if event.rate_limit_info and self.account is not None:
            _apply_limit_info(self.account, event.rate_limit_info)
            await db.commit()
        if event.available_commands and not sess.available_commands:
            sess.available_commands = event.available_commands
            await db.commit()
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
            self.seq += 1
            db.add(
                Event(
                    run_id=run.id,
                    session_id=sess.id,
                    seq=self.seq,
                    kind=event.kind,
                    text=event.text,
                    tool_name=event.tool_name,
                    raw=event.raw,
                    parent_tool_use_id=event.parent_tool_use_id,
                    agent_name=event.agent_name,
                )
            )
            await db.commit()
            payload["seq"] = self.seq

        hub.publish(sess.id, payload)

        # A sandbox that cannot start looks, in the transcript, exactly like a
        # command that failed on its own. Say what it actually is, once.
        if event.text and not self._warned_bwrap and _BWRAP_ERROR.search(event.text):
            self._warned_bwrap = True
            log.warning("run %s: %s", run.id, BWRAP_HINT)
            await self.emit(
                NormalizedEvent(kind="system", text=BWRAP_HINT, raw={"aiops_hint": "bubblewrap"})
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

    An isolated agent runs as another user, which the app cannot signal at all,
    so this goes through the same helper that started it.
    """
    agent_env.signal_agent(proc, sig)


def _terminate(proc: asyncio.subprocess.Process) -> None:
    _signal_group(proc, signal.SIGTERM)


async def _wait_quietly(proc: asyncio.subprocess.Process) -> int | None:
    try:
        return await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        _signal_group(proc, _SIGKILL)
        return await proc.wait()


def _apply_limit_info(account: ProviderAccount, info: dict) -> None:
    """Record the plan window the CLI reported against this account."""
    status = str(info.get("status") or "").lower()
    account.limit_status = status or None
    account.limit_window = str(info.get("rateLimitType") or "") or None
    account.limit_seen_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    resets = info.get("resetsAt")
    if isinstance(resets, (int, float)) and resets > 0:
        account.limit_resets_at = datetime.fromtimestamp(resets, tz=timezone.utc)
    # Anything other than "allowed" means the window is refusing work; hold the
    # account out until it resets so the next turn goes to the fallback.
    if status and status != "allowed":
        resets_at = _aware(account.limit_resets_at)
        # Only trust a reset time that is actually in the future. A stale one
        # left by an earlier event would put limited_until in the past, which
        # lets the exhausted account be picked again immediately.
        account.limited_until = (
            resets_at
            if resets_at and resets_at > now
            else now + timedelta(seconds=settings.account_limit_cooldown_seconds)
        )


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
