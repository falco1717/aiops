from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import datetime, timezone

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .events import hub
from .models import Event, Run, Session
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
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if run is None or run.status != "queued":
                return
            sess = await db.get(Session, run.session_id)
            if sess is None:
                await self._finalize(run_id, "failed", None, "Session no longer exists")
                return

            provider = get_provider(sess.provider)
            preset = sess.preset
            workspace = sess.workspace
            cwd = workspace.path if workspace else settings.workspace_root
            if not os.path.isdir(cwd):
                await self._finalize(run_id, "failed", None, f"Workspace directory missing: {cwd}")
                return

            spec = provider.build_run(
                prompt=run.prompt,
                model=sess.model or (preset.model if preset else None),
                provider_session_id=sess.provider_session_id,
                permission_mode=preset.permission_mode if preset else None,
                system_prompt=preset.system_prompt if preset else None,
                allowed_tools=preset.allowed_tools if preset else None,
                extra_args=(preset.extra_args if preset else []) or [],
                stream_partials=settings.stream_partial_messages,
            )

            if spec.assigned_session_id and not sess.provider_session_id:
                sess.provider_session_id = spec.assigned_session_id

            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            run.command = spec.argv
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

        await self._finalize(run_id, status, exit_code, state.error, state.cost_usd)

    async def _pump_stdout(self, db, run: Run, sess: Session, provider, proc, state) -> None:
        seq = 0
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
    ) -> None:
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.exit_code = exit_code
            run.finished_at = datetime.now(timezone.utc)
            if error:
                run.error = error[:8000]
            if cost_usd is not None:
                run.cost_usd = cost_usd
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


def _redact(argv: list[str]) -> list[str]:
    """The prompt can be long; show the shape of the command, not the payload."""
    out = []
    for arg in argv:
        out.append(arg if len(arg) <= 120 else arg[:117] + "...")
    return out


runner = Runner()
