from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from .config import settings

log = logging.getLogger("aiops.auth_flows")

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
URL = re.compile(r"https://[^\s\x1b'\"]+")
# Codex device codes look like ABCD-EFGHI.
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")

# Codex says the code expires in 15 minutes; give the whole flow a little longer.
FLOW_TIMEOUT_SECONDS = 16 * 60

Status = Literal[
    "starting",       # process spawned, nothing parsed yet
    "awaiting_user",  # we have a URL (and code) for the operator to act on
    "completing",     # operator says they finished; waiting on the CLI
    "success",
    "failed",
    "cancelled",
    "expired",
]


@dataclass
class LoginFlow:
    """One in-progress provider sign-in, driven from the web UI.

    The operator authenticates on the provider's own site. All AIOps ever
    handles is the verification URL, the device code, and (for Claude) the
    short-lived authorization code pasted back — never an account password.
    """

    provider: str
    status: Status = "starting"
    verification_url: str | None = None
    user_code: str | None = None
    # Claude blocks on stdin for an authorization code; Codex polls by itself.
    needs_code: bool = False
    message: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    _proc: asyncio.subprocess.Process | None = None
    _output: str = ""

    @property
    def expires_in(self) -> int:
        return max(0, int(FLOW_TIMEOUT_SECONDS - (time.monotonic() - self.started_at)))

    def public(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "needs_code": self.needs_code,
            "message": self.message,
            "expires_in": self.expires_in,
        }


class LoginManager:
    """Holds at most one in-flight sign-in per provider."""

    def __init__(self) -> None:
        self._flows: dict[str, LoginFlow] = {}
        self._lock = asyncio.Lock()

    def get(self, provider: str) -> LoginFlow | None:
        flow = self._flows.get(provider)
        if flow and flow.status in ("starting", "awaiting_user", "completing"):
            if flow.expires_in == 0:
                flow.status = "expired"
                flow.message = "The sign-in window expired. Start again."
                self._kill(flow)
        return flow

    async def start(
        self, provider: str, key: str, account_env: dict[str, str] | None = None
    ) -> LoginFlow:
        """Begin a sign-in. `key` scopes the flow to one account."""
        async with self._lock:
            existing = self.get(key)
            if existing and existing.status in ("starting", "awaiting_user", "completing"):
                return existing

            argv = self._argv(provider)
            flow = LoginFlow(provider=provider)
            try:
                flow._proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._env(account_env),
                    start_new_session=True,
                )
            except FileNotFoundError:
                flow.status = "failed"
                flow.message = f"{argv[0]} is not installed in this container."
                self._flows[key] = flow
                return flow

            self._flows[key] = flow
            asyncio.create_task(self._pump(flow), name=f"login-{key}")
            # Give the CLI a moment to emit its URL so the first response is useful.
            for _ in range(60):
                await asyncio.sleep(0.1)
                if flow.verification_url or flow.status in ("failed", "success"):
                    break
            return flow

    async def submit_code(self, key: str, code: str) -> LoginFlow:
        """Feed Claude's authorization code to the waiting process."""
        flow = self.get(key)
        if flow is None or flow.status != "awaiting_user":
            raise ValueError("No sign-in is waiting for a code. Start one first.")
        if not flow.needs_code:
            raise ValueError(
                f"{flow.provider} completes in the browser — there is no code to paste here."
            )
        proc = flow._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise ValueError("The sign-in process is no longer running. Start again.")

        code = code.strip()
        if not code:
            raise ValueError("Authorization code must not be empty")
        try:
            proc.stdin.write((code + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ValueError("The sign-in process closed before the code arrived.") from exc

        flow.status = "completing"
        flow.message = "Verifying with the provider…"
        return flow

    async def cancel(self, key: str) -> None:
        flow = self._flows.get(key)
        if flow is None:
            return
        self._kill(flow)
        if flow.status in ("starting", "awaiting_user", "completing"):
            flow.status = "cancelled"
            flow.message = "Sign-in cancelled."

    async def logout(
        self, provider: str, key: str, account_env: dict[str, str] | None = None
    ) -> tuple[bool, str]:
        argv = [settings.claude_bin, "auth", "logout"] if provider == "claude" else [
            settings.codex_bin,
            "logout",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(account_env),
            )
        except FileNotFoundError:
            return False, f"{argv[0]} is not installed."
        out, _ = await proc.communicate()
        self._flows.pop(key, None)
        return proc.returncode == 0, _clean(out.decode("utf-8", "replace"))[:500]

    # -- internals -----------------------------------------------------
    @staticmethod
    def _argv(provider: str) -> list[str]:
        if provider == "claude":
            # Prints an authorize URL, then blocks on stdin for the code.
            return [settings.claude_bin, "auth", "login"]
        if provider == "codex":
            # Prints a URL + device code and polls until the operator approves.
            return [settings.codex_bin, "login", "--device-auth"]
        raise ValueError(f"Unknown provider {provider!r}")

    @staticmethod
    def _env(account_env: dict[str, str] | None = None) -> dict[str, str]:
        import os

        # Force the non-graphical path; there is no browser in the container.
        # account_env carries CLAUDE_CONFIG_DIR / CODEX_HOME so the credentials
        # land in the right account's directory.
        return {
            **os.environ,
            **(account_env or {}),
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "TERM": "dumb",
            "BROWSER": "true",
            "DISPLAY": "",
        }

    async def _pump(self, flow: LoginFlow) -> None:
        """Read the CLI's output as it arrives and drive the flow's state.

        Reads raw chunks rather than lines: both CLIs end with an unterminated
        prompt, which readline() would block on forever.
        """
        proc = flow._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=5)
                except asyncio.TimeoutError:
                    if flow.expires_in == 0:
                        flow.status = "expired"
                        flow.message = "The sign-in window expired. Start again."
                        self._kill(flow)
                        return
                    continue
                if not chunk:
                    break
                flow._output += _clean(chunk.decode("utf-8", "replace"))
                self._parse(flow)

            code = await proc.wait()
            tail = flow._output.strip().splitlines()
            if code == 0:
                flow.status = "success"
                flow.message = "Signed in."
            elif flow.status not in ("cancelled", "expired"):
                flow.status = "failed"
                flow.message = (tail[-1][:300] if tail else f"CLI exited with code {code}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("login flow for %s crashed", flow.provider)
            flow.status = "failed"
            flow.message = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _parse(flow: LoginFlow) -> None:
        text = flow._output
        if flow.verification_url is None:
            for match in URL.finditer(text):
                url = match.group(0).rstrip(".,)")
                # Skip docs/marketing links; we want the authorize endpoint.
                if any(k in url for k in ("oauth", "device", "authorize", "login")):
                    flow.verification_url = url
                    break

        if flow.provider == "codex" and flow.user_code is None:
            after = text.split("one-time code", 1)
            hunt = after[1] if len(after) > 1 else text
            found = DEVICE_CODE.search(hunt)
            if found:
                flow.user_code = found.group(0)

        if flow.provider == "claude":
            flow.needs_code = "aste code" in text  # "Paste code here if prompted >"

        if flow.verification_url and flow.status == "starting":
            flow.status = "awaiting_user"
            flow.message = (
                "Open the link, approve the sign-in, then paste the code you are given."
                if flow.provider == "claude"
                else "Open the link and enter the code shown."
            )

    @staticmethod
    def _kill(flow: LoginFlow) -> None:
        proc = flow._proc
        if proc is None or proc.returncode is not None:
            return
        import os
        import signal

        if hasattr(os, "killpg"):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()


def _clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r", "")


login_manager = LoginManager()
