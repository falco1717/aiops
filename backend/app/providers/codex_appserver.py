"""Drives `codex app-server` so a run can stop and ask a human before acting.

`codex exec` can never ask anything: `--ask-for-approval` is rejected there and
forcing `-c approval_policy=untrusted` still reports "approval: never". The
app-server is the only surface on this binary that turns an approval into a
round trip, so interactive sessions go through here instead of `codex.py`.

The transport is newline-delimited JSON-RPC 2.0 over the process's stdin and
stdout. Method names, parameter names and decision enums below were taken from
`codex app-server generate-json-schema` (codex-cli 0.147.0) rather than from
documentation — the earlier `newConversation`/`sendUserTurn` protocol has been
replaced by `thread/start` and `turn/start`, and the approval requests are now
namespaced under `item/`. Both the current requests and the legacy v1 ones
(`execCommandApproval`, `applyPatchApproval`) are answered, because the server
still emits the old pair for some tool paths.

Everything here fails closed. A dead process, an unparseable frame, a callback
that raises, or a human who never answers all end as a denial plus a clean
shutdown; nothing waits forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..agent_env import agent_environ
from ..config import settings
from .base import NormalizedEvent

#: Called when Codex wants a human decision.
#:
#: ``(kind, tool_name, summary, request) -> (allowed, note)`` where *kind* is one
#: of the KIND_* constants, *summary* is one line fit to show an operator, and
#: *request* is the raw JSON-RPC params so a caller can render more detail.
ApprovalCallback = Callable[
    [str, "str | None", str, dict[str, Any]], Awaitable[tuple[bool, "str | None"]]
]


class CodexAppServerError(RuntimeError):
    """The app-server refused a request or died mid-conversation."""


# --- approval kinds handed to the callback ---------------------------------
KIND_COMMAND = "command_execution"
KIND_FILE_CHANGE = "file_change"
KIND_PERMISSIONS = "permissions"
KIND_EXEC_COMMAND = "exec_command"  # legacy v1 equivalent of KIND_COMMAND
KIND_APPLY_PATCH = "apply_patch"  # legacy v1 equivalent of KIND_FILE_CHANGE

#: Server->client requests that mean "a human must decide", mapped to the kind
#: and the tool label the UI already uses for that sort of action.
APPROVAL_METHODS: dict[str, tuple[str, str | None]] = {
    "item/commandExecution/requestApproval": (KIND_COMMAND, "shell"),
    "item/fileChange/requestApproval": (KIND_FILE_CHANGE, "edit"),
    "item/permissions/requestApproval": (KIND_PERMISSIONS, "permissions"),
    "execCommandApproval": (KIND_EXEC_COMMAND, "shell"),
    "applyPatchApproval": (KIND_APPLY_PATCH, "edit"),
}

#: Sandbox tier per AIOps approval mode. "ask" still runs sandboxed — the human
#: gates the escapes — but a session that has opted out of prompts needs the
#: tier to carry the risk instead.
SANDBOX_MODES = {
    "ask": "workspace-write",
    "auto": "workspace-write",
    "bypass": "danger-full-access",
}

#: Codex wording when a plan's quota is exhausted, so the runner can fail over.
#: Kept local rather than imported from `codex.py` so this adapter cannot be
#: broken by a rename in the `codex exec` parser.
_LIMIT_PATTERNS = re.compile(
    r"(usage limit|rate limit|rate_limit|quota|too many requests|429"
    r"|limit reached|try again (?:later|in))",
    re.IGNORECASE,
)


class CodexAppServerAdapter:
    """One interactive Codex turn, from process spawn to final result.

    Construct it, iterate `run()`, then read `conversation_id` (store it so the
    next turn can resume) and `usage`. Constructing does not touch the process,
    so the translation helpers can be unit-tested without the binary.
    """

    #: Announced to the server during `initialize`.
    CLIENT_NAME = "aiops"
    CLIENT_VERSION = "1"

    def __init__(
        self,
        *,
        prompt: str,
        cwd: str,
        model: str | None = None,
        sandbox: str = "workspace-write",
        codex_home: str | None = None,
        on_approval: ApprovalCallback | None = None,
        resume_id: str | None = None,
        system_prompt: str | None = None,
        approval_policy: str = "untrusted",
        config_overrides: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        codex_bin: str | None = None,
        stream_partials: bool = False,
        timeout: float | None = None,
        approval_timeout: float | None = None,
        startup_timeout: float = 90.0,
    ) -> None:
        self.prompt = prompt
        self.cwd = cwd
        self.model = model
        self.sandbox = sandbox
        self.codex_home = codex_home
        self.on_approval = on_approval
        self.resume_id = resume_id
        self.system_prompt = system_prompt
        # "untrusted" asks about everything Codex has not itself decided is
        # safe; "on-request" only asks when the sandbox blocks something.
        self.approval_policy = approval_policy
        self.config_overrides = dict(config_overrides or {})
        self.extra_env = dict(env or {})
        self.codex_bin = codex_bin or settings.codex_bin
        self.stream_partials = stream_partials
        self.timeout = float(timeout if timeout is not None else settings.run_timeout_seconds)
        self.approval_timeout = float(
            approval_timeout if approval_timeout is not None else settings.approval_timeout_seconds
        )
        self.startup_timeout = float(startup_timeout)

        # --- results the caller reads afterwards ---
        self.conversation_id: str | None = resume_id
        self.turn_id: str | None = None
        self.usage: dict[str, int] | None = None
        self.approvals: list[dict[str, Any]] = []
        self.stderr_tail: list[str] = []

        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._closed = False
        self._deadline = 0.0
        # Last-seen payload per item id. A file-change approval names only its
        # itemId, so the paths it wants to touch have to be recovered from the
        # `item/started` notification that preceded it.
        self._items: dict[str, dict[str, Any]] = {}

    # -- public ---------------------------------------------------------
    async def run(self) -> AsyncIterator[NormalizedEvent]:
        """Yield the turn's events, asking `on_approval` whenever Codex asks."""
        self._deadline = time.monotonic() + self.timeout
        try:
            async for event in self._run():
                yield event
        except asyncio.CancelledError:
            raise
        except CodexAppServerError as exc:
            yield self._error_event(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            yield self._error_event(f"{type(exc).__name__}: {exc}")
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop the app-server and release everything, safe to call twice."""
        if self._closed:
            return
        self._closed = True
        for task in (self._reader, self._stderr_reader):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexAppServerError("app-server shut down"))
        self._pending.clear()
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

    # -- the turn -------------------------------------------------------
    async def _run(self) -> AsyncIterator[NormalizedEvent]:
        await self._spawn()

        await self._call(
            "initialize",
            {
                "clientInfo": {"name": self.CLIENT_NAME, "version": self.CLIENT_VERSION},
                # Without this the server hides the `thread/*` and `turn/*`
                # methods this adapter is built on.
                "capabilities": {"experimentalApi": True},
            },
            timeout=self.startup_timeout,
        )
        self._notify("initialized", None)

        yield await self._open_thread()

        turn = self._call_async(
            "turn/start",
            {
                "threadId": self.conversation_id,
                "input": [{"type": "text", "text": self._full_prompt()}],
                "approvalPolicy": self.approval_policy,
            },
        )

        # `turn/start` answers as soon as the turn is accepted, with the turn
        # still `inProgress`, so its response is not the end of the turn — the
        # `turn/completed` notification is. The response is still watched
        # because a rejected turn only ever surfaces as a JSON-RPC error.
        turn_seen = False
        grace_until = 0.0
        get_task: asyncio.Task[dict[str, Any] | None] = asyncio.ensure_future(self._inbox.get())
        try:
            while True:
                now = time.monotonic()
                budget = self._deadline - now
                if budget <= 0:
                    yield self._error_event(
                        f"codex app-server exceeded the {self.timeout:.0f}s run timeout"
                    )
                    await self._interrupt()
                    return
                if grace_until:
                    budget = min(budget, max(grace_until - now, 0.0))

                waiting: set[asyncio.Future[Any]] = {get_task}
                if not turn.done():
                    waiting.add(turn)
                done, _ = await asyncio.wait(
                    waiting, timeout=max(budget, 0.05), return_when=asyncio.FIRST_COMPLETED
                )

                if turn.done() and not turn_seen:
                    turn_seen = True
                    exc = turn.exception()
                    if exc is not None:
                        yield self._error_event(str(exc))
                        return
                    started = (turn.result() or {}).get("turn") or {}
                    if isinstance(started, dict):
                        if isinstance(started.get("id"), str):
                            self.turn_id = started["id"]
                        if started.get("status") in ("completed", "failed", "interrupted"):
                            # Already over on arrival; drain what is queued
                            # behind it rather than cutting the tail off.
                            grace_until = time.monotonic() + 2.0

                if get_task in done:
                    message = get_task.result()
                    get_task = asyncio.ensure_future(self._inbox.get())
                    if message is None:
                        yield self._error_event(self._death_reason())
                        return
                    finished = False
                    for event in await self._handle(message):
                        if event is _TURN_OVER:
                            finished = True
                            continue
                        yield event
                    if finished:
                        return
                    continue

                if not done and grace_until:
                    return
        finally:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await get_task

    async def _open_thread(self) -> NormalizedEvent:
        params: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
            # Route approvals to us rather than to Codex's own auto-reviewer
            # subagent, which would silently answer on the human's behalf.
            "approvalsReviewer": "user",
        }
        if self.model:
            params["model"] = self.model
        if self.config_overrides:
            params["config"] = self.config_overrides

        if self.resume_id:
            params["threadId"] = self.resume_id
            method = "thread/resume"
        else:
            method = "thread/start"

        result = await self._call(method, params, timeout=self.startup_timeout)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = (thread or {}).get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError(f"{method} returned no thread id: {_stringify(result)}")
        self.conversation_id = thread_id
        label = "session resumed" if self.resume_id else "session started"
        return NormalizedEvent(
            kind="system",
            text=label,
            raw={"method": method, "result": result},
            provider_session_id=thread_id,
        )

    def _full_prompt(self) -> str:
        # Codex has no --append-system-prompt, and `developerInstructions` only
        # applies at thread creation, so preset instructions ride the prompt the
        # same way `codex exec` does it.
        if self.system_prompt:
            return f"{self.system_prompt.strip()}\n\n---\n\n{self.prompt}"
        return self.prompt

    # -- inbound routing ------------------------------------------------
    async def _handle(self, message: dict[str, Any]) -> list[Any]:
        method = message.get("method")
        if not isinstance(method, str):
            return []
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if "id" in message:
            return await self._handle_server_request(message, method, params)
        return self._translate(method, params)

    async def _handle_server_request(
        self, message: dict[str, Any], method: str, params: dict[str, Any]
    ) -> list[Any]:
        request_id = message.get("id")
        if method not in APPROVAL_METHODS:
            if method == "item/tool/requestUserInput":
                # A free-text question, not an allow/deny. There is nobody in
                # this transport to type an answer, so answer nothing rather
                # than leave the turn parked forever.
                self._respond(request_id, {"answers": {}})
                return [
                    NormalizedEvent(
                        kind="system",
                        text="codex asked for free-text input; answered with nothing",
                        raw=message,
                        provider_session_id=self.conversation_id,
                    )
                ]
            self._respond_error(request_id, -32601, f"{method} is not supported by this client")
            return [
                NormalizedEvent(
                    kind="system",
                    text=f"declined unsupported server request {method}",
                    raw=message,
                    provider_session_id=self.conversation_id,
                )
            ]

        kind, tool_name = APPROVAL_METHODS[method]
        item = self._items.get(str(params.get("itemId") or ""))
        summary = approval_summary(method, params, item)
        events: list[Any] = [
            NormalizedEvent(
                kind="system",
                text=f"approval requested ({kind}): {summary}",
                tool_name=tool_name,
                raw=message,
                provider_session_id=self.conversation_id,
            )
        ]

        allowed, note = await self._ask(kind, tool_name, summary, params)
        self.approvals.append(
            {"kind": kind, "summary": summary, "allowed": allowed, "note": note}
        )
        self._respond(request_id, approval_reply(method, params, allowed, note))
        verdict = "allowed" if allowed else "denied"
        events.append(
            NormalizedEvent(
                kind="system",
                text=f"approval {verdict}: {summary}" + (f" — {note}" if note else ""),
                tool_name=tool_name,
                raw={"method": method, "allowed": allowed, "note": note},
                provider_session_id=self.conversation_id,
            )
        )
        return events

    async def _ask(
        self, kind: str, tool_name: str | None, summary: str, params: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Get a human decision, defaulting to denial for every failure mode."""
        if self.on_approval is None:
            return False, "no approval handler is attached to this run"
        try:
            allowed, note = await asyncio.wait_for(
                self.on_approval(kind, tool_name, summary, params),
                timeout=self.approval_timeout,
            )
        except asyncio.TimeoutError:
            return False, f"no answer within {self.approval_timeout:g}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, f"approval handler failed: {type(exc).__name__}: {exc}"
        return bool(allowed), (note if isinstance(note, str) and note else None)

    # -- protocol -> NormalizedEvent ------------------------------------
    def _translate(self, method: str, params: dict[str, Any]) -> list[Any]:
        """Map one server notification onto the shared event shape.

        Pure apart from recording the thread id and token usage, so tests can
        drive it with captured payloads and no subprocess.
        """
        sid = self.conversation_id

        if method == "thread/started":
            thread = params.get("thread") or {}
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                self.conversation_id = thread["id"]
            return [
                NormalizedEvent(
                    kind="system",
                    text="session started",
                    raw=params,
                    provider_session_id=self.conversation_id,
                )
            ]

        if method == "turn/started":
            turn = params.get("turn") or {}
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self.turn_id = turn["id"]
            return []  # no information beyond the status we already track

        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") if isinstance(usage, dict) else None
            if isinstance(last, dict):
                self.usage = _usage(last)
            return []

        if method == "turn/completed":
            turn = params.get("turn") or {}
            status = turn.get("status") if isinstance(turn, dict) else None
            error = turn.get("error") if isinstance(turn, dict) else None
            if status == "failed" or isinstance(error, dict):
                text = _stringify((error or {}).get("message") or error or "turn failed")
                return [self._error_event(text, raw=params), _TURN_OVER]
            return [
                NormalizedEvent(
                    kind="result",
                    text=_last_agent_text(turn) or str(status or "completed"),
                    raw=params,
                    provider_session_id=sid,
                    usage=self.usage,
                ),
                _TURN_OVER,
            ]

        if method == "error":
            error = params.get("error") if isinstance(params.get("error"), dict) else params
            text = _stringify(error.get("message") or error)
            event = self._error_event(text, raw=params)
            # A retryable error is a status update, not the end of the turn.
            return [event] if params.get("willRetry") else [event, _TURN_OVER]

        if method in ("item/started", "item/completed"):
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if isinstance(item.get("id"), str):
                if len(self._items) > 200:
                    self._items.clear()
                self._items[item["id"]] = item
            event = self._item(item, params, started=method == "item/started")
            return [event] if event else []

        if method == "item/agentMessage/delta":
            return self._delta(params.get("delta"), params)
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            return self._delta(params.get("delta") or params.get("text"), params)
        if method == "item/commandExecution/outputDelta":
            return self._delta(_decode_chunk(params), params)

        if method in ("warning", "guardianWarning", "configWarning", "deprecationNotice"):
            return [
                NormalizedEvent(
                    kind="system",
                    text=_stringify(params.get("summary") or params.get("message") or params),
                    raw=params,
                    provider_session_id=sid,
                )
            ]

        if method in _QUIET_NOTIFICATIONS:
            return []

        return [
            NormalizedEvent(kind="system", text=method, raw=params, provider_session_id=sid)
        ]

    def _item(
        self, item: dict[str, Any], params: dict[str, Any], started: bool
    ) -> NormalizedEvent | None:
        itype = str(item.get("type") or "")
        sid = self.conversation_id

        if itype == "agentMessage":
            if started:
                return None  # the completed event carries the text
            text = _stringify(item.get("text"))
            return (
                NormalizedEvent(kind="assistant", text=text, raw=params, provider_session_id=sid)
                if text
                else None
            )

        if itype == "reasoning":
            if started:
                return None
            text = _stringify(item.get("summary") or item.get("content"))
            return (
                NormalizedEvent(kind="thinking", text=text, raw=params, provider_session_id=sid)
                if text
                else None
            )

        if itype == "commandExecution":
            command = _stringify(item.get("command"))
            if started:
                return NormalizedEvent(
                    kind="tool_use",
                    tool_name="shell",
                    text=command,
                    raw=params,
                    provider_session_id=sid,
                )
            status = item.get("status")
            exit_code = item.get("exitCode")
            output = _stringify(item.get("aggregatedOutput"))
            if status == "declined" and not output:
                output = f"denied by the operator: {command}"
            return NormalizedEvent(
                kind="tool_result",
                text=output or command,
                tool_name="shell",
                raw=params,
                is_error=bool(exit_code) or status in ("failed", "declined"),
                provider_session_id=sid,
            )

        if itype == "fileChange":
            if started:
                return NormalizedEvent(
                    kind="tool_use",
                    tool_name="edit",
                    text=_stringify(item.get("changes")),
                    raw=params,
                    provider_session_id=sid,
                )
            status = item.get("status")
            return NormalizedEvent(
                kind="tool_result",
                tool_name="edit",
                text=_stringify(item.get("changes")),
                raw=params,
                is_error=status in ("failed", "declined"),
                provider_session_id=sid,
            )

        if itype in ("mcpToolCall", "dynamicToolCall"):
            name = str(item.get("tool") or itype)
            if started:
                return NormalizedEvent(
                    kind="tool_use",
                    tool_name=name,
                    text=_stringify(item.get("arguments")),
                    raw=params,
                    provider_session_id=sid,
                )
            return NormalizedEvent(
                kind="tool_result",
                tool_name=name,
                text=_stringify(item.get("result") or item.get("error") or item.get("contentItems")),
                raw=params,
                is_error=item.get("status") == "failed" or item.get("success") is False,
                provider_session_id=sid,
            )

        if itype == "webSearch":
            if started:
                return None
            return NormalizedEvent(
                kind="tool_use",
                tool_name="web_search",
                text=_stringify(item.get("query")),
                raw=params,
                provider_session_id=sid,
            )

        if itype == "plan":
            if started:
                return None
            return NormalizedEvent(
                kind="system",
                text=_stringify(item.get("text")),
                raw=params,
                provider_session_id=sid,
            )

        if itype == "userMessage":
            return None  # our own prompt echoed back

        if started:
            return None
        return NormalizedEvent(
            kind="system", text=itype or "item", raw=params, provider_session_id=sid
        )

    def _delta(self, text: Any, params: dict[str, Any]) -> list[NormalizedEvent]:
        if not self.stream_partials:
            return []
        body = _stringify(text, limit=2000)
        if not body:
            return []
        return [
            NormalizedEvent(
                kind="delta",
                text=body,
                raw=params,
                persist=False,
                provider_session_id=self.conversation_id,
            )
        ]

    def _error_event(self, text: str, raw: dict[str, Any] | None = None) -> NormalizedEvent:
        return NormalizedEvent(
            kind="error",
            text=text,
            raw=raw or {"error": text},
            is_error=True,
            rate_limited=looks_rate_limited(text),
            provider_session_id=self.conversation_id,
        )

    # -- transport ------------------------------------------------------
    async def _spawn(self) -> None:
        env = agent_environ()
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        env.update(self.extra_env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                # Command output arrives inline; the 64KiB default would abort
                # the reader on a single chatty frame.
                limit=8 * 1024 * 1024,
            )
        except OSError as exc:
            raise CodexAppServerError(f"could not start `{self.codex_bin} app-server`: {exc}") from exc
        self._reader = asyncio.ensure_future(self._read_stdout())
        self._stderr_reader = asyncio.ensure_future(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        stream = self._proc.stdout
        try:
            while True:
                try:
                    line = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # An oversized frame: we can no longer trust the stream
                    # position, so end the conversation rather than misparse.
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    self.stderr_tail.append(f"unparseable stdout: {text[:400]}")
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    await self._inbox.put(message)
                else:
                    self._resolve(message)
        except asyncio.CancelledError:
            raise
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(CodexAppServerError(self._death_reason()))
            with contextlib.suppress(Exception):
                self._inbox.put_nowait(None)

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        stream = self._proc.stderr
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
                del self.stderr_tail[:-40]
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - diagnostics only
            return

    def _resolve(self, message: dict[str, Any]) -> None:
        key = message.get("id")
        future = self._pending.pop(key if isinstance(key, int) else -1, None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                CodexAppServerError(
                    f"{_stringify(error.get('message') or error)}"
                    + (f" ({error.get('code')})" if isinstance(error, dict) else "")
                )
            )
        else:
            future.set_result(message.get("result"))

    def _call_async(self, method: str, params: dict[str, Any] | None) -> asyncio.Future[Any]:
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            self._write(frame)
        except CodexAppServerError as exc:
            self._pending.pop(request_id, None)
            future.set_exception(exc)
        return future

    async def _call(
        self, method: str, params: dict[str, Any] | None, timeout: float | None = None
    ) -> Any:
        future = self._call_async(method, params)
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.startup_timeout)
        except asyncio.TimeoutError as exc:
            raise CodexAppServerError(f"{method} timed out after {timeout or self.startup_timeout:.0f}s") from exc

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        frame["params"] = params if params is not None else {}
        with contextlib.suppress(CodexAppServerError):
            self._write(frame)

    def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        with contextlib.suppress(CodexAppServerError):
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        with contextlib.suppress(CodexAppServerError):
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )

    def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise CodexAppServerError(self._death_reason())
        proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))

    async def _interrupt(self) -> None:
        if self.conversation_id and self.turn_id:
            with contextlib.suppress(Exception):
                self._notify("turn/interrupt", {"threadId": self.conversation_id, "turnId": self.turn_id})

    def _death_reason(self) -> str:
        code = self._proc.returncode if self._proc else None
        tail = "; ".join(self.stderr_tail[-5:])
        base = f"codex app-server exited (code {code})" if code is not None else "codex app-server closed its stream"
        return f"{base}: {tail}" if tail else base


#: Sentinel yielded by the translator to say the turn is finished. Never leaves
#: the adapter — `run()` filters it out.
_TURN_OVER = object()

#: Notifications that carry nothing an operator would want in the transcript.
_QUIET_NOTIFICATIONS = frozenset(
    {
        "thread/status/changed",
        "thread/settings/updated",
        "thread/name/updated",
        "turn/diff/updated",
        "turn/plan/updated",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "serverRequest/resolved",
        "account/updated",
        "account/rateLimits/updated",
        "mcpServer/startupStatus/updated",
        "remoteControl/status/changed",
        "thread/goal/updated",
        "thread/goal/cleared",
        "thread/environment/connected",
        "thread/environment/disconnected",
        "fs/changed",
        "item/autoApprovalReview/started",
        "item/autoApprovalReview/completed",
    }
)


# --- approval translation (pure, so the tests can pin it) -------------------
def approval_summary(
    method: str, params: dict[str, Any], item: dict[str, Any] | None = None
) -> str:
    """One line describing what Codex is asking permission to do.

    *item* is the `item/started` payload this approval belongs to, when one was
    seen. A file-change request carries only an itemId, so without it the
    operator would be asked to approve "some edit" with no paths.
    """
    if method == "item/commandExecution/requestApproval":
        command = _stringify(params.get("command")) or _stringify(params.get("commandActions"))
        cwd = params.get("cwd")
        reason = params.get("reason")
        parts = [command or "(no command)"]
        if cwd:
            parts.append(f"in {cwd}")
        if reason:
            parts.append(f"({reason})")
        return " ".join(parts)

    if method == "execCommandApproval":
        command = params.get("command")
        text = " ".join(str(c) for c in command) if isinstance(command, list) else _stringify(command)
        parts = [text or "(no command)"]
        if params.get("cwd"):
            parts.append(f"in {params['cwd']}")
        if params.get("reason"):
            parts.append(f"({params['reason']})")
        return " ".join(parts)

    if method == "item/fileChange/requestApproval":
        paths = _change_paths(item)
        reason = params.get("reason")
        head = f"apply file changes to {', '.join(paths)}" if paths else "apply file changes"
        root = params.get("grantRoot")
        if root:
            head += f" under {root}"
        return f"{head} ({reason})" if reason else head

    if method == "applyPatchApproval":
        changes = params.get("changes") or params.get("fileChanges")
        paths = sorted(changes) if isinstance(changes, dict) else None
        return "apply patch to " + (", ".join(paths) if paths else _stringify(changes) or "(unknown files)")

    if method == "item/permissions/requestApproval":
        reason = params.get("reason") or "escalate permissions"
        return f"{reason}: {_stringify(params.get('permissions'), limit=400)}"

    return _stringify(params, limit=400) or method


def approval_reply(
    method: str, params: dict[str, Any], allowed: bool, note: str | None
) -> dict[str, Any]:
    """Build the JSON-RPC result for one approval request.

    The decision vocabularies differ per request type and are quoted from the
    generated schema:

    * `CommandExecutionApprovalDecision` / `FileChangeApprovalDecision`:
      `accept`, `acceptForSession`, `decline`, `cancel`. Neither carries a
      rejection string, so a denial note is only advisory here.
    * `ReviewDecision` (the legacy `execCommandApproval` / `applyPatchApproval`
      pair): `approved`, `approved_for_session`, `timed_out`, `abort`, and
      `{"denied": {"rejection": "..."}}` — the one shape that does carry the
      operator's reason back to the model.
    * `item/permissions/requestApproval` answers with a granted profile rather
      than a decision, so a denial grants nothing.

    The live server also sends an `availableDecisions` list that the generated
    schema does not mention, and it omits `decline` — but replying `decline`
    was verified to work against 0.147.0 and is the behaviour we want, because
    the listed alternative (`cancel`) tears the whole turn down instead of
    letting the model try another route after a refusal.
    """
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        # "decline" rather than "cancel": the model should be told no and pick
        # another route, not have the whole turn torn down.
        return {"decision": "accept" if allowed else "decline"}

    if method in ("execCommandApproval", "applyPatchApproval"):
        if allowed:
            return {"decision": "approved"}
        return {"decision": {"denied": {"rejection": note or "denied by the operator"}}}

    if method == "item/permissions/requestApproval":
        requested = params.get("permissions")
        granted = requested if (allowed and isinstance(requested, dict)) else {}
        return {"permissions": granted, "scope": "turn"}

    return {"decision": "accept" if allowed else "decline"}


def _change_paths(item: dict[str, Any] | None) -> list[str]:
    if not isinstance(item, dict):
        return []
    paths = []
    for change in item.get("changes") or []:
        if isinstance(change, dict):
            kind = change.get("kind")
            verb = kind.get("type") if isinstance(kind, dict) else kind
            path = str(change.get("path") or "")
            if path:
                paths.append(f"{path} ({verb})" if verb else path)
    return paths[:10]


def looks_rate_limited(text: str) -> bool:
    return bool(text) and bool(_LIMIT_PATTERNS.search(text))


def _usage(breakdown: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(breakdown.get("inputTokens") or 0),
        "output_tokens": int(breakdown.get("outputTokens") or 0),
        "cache_read_tokens": int(breakdown.get("cachedInputTokens") or 0),
        "cache_write_tokens": int(breakdown.get("cacheWriteInputTokens") or 0),
    }


def _last_agent_text(turn: Any) -> str:
    if not isinstance(turn, dict):
        return ""
    for item in reversed(turn.get("items") or []):
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            return _stringify(item.get("text"))
    return ""


def _decode_chunk(params: dict[str, Any]) -> str:
    chunk = params.get("chunk") or params.get("delta") or params.get("text")
    if isinstance(chunk, list):
        with contextlib.suppress(Exception):
            return bytes(chunk).decode("utf-8", "replace")
    return _stringify(chunk, limit=2000)


def _stringify(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(_stringify(item, limit))
        text = "\n".join(p for p in parts if p)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"
