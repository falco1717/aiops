#!/usr/bin/env python3
"""A stdio MCP server that lets an agent open a GitHub pull request.

Same shape as `mcp_browser.py`, cut down to the one thing this needs: no
Chromium, no proxy, no page. **What it may learn.** The workspace's linked
GitHub token is never handed to the agent. This process — the agent's own
child, at the agent's uid — asks AIOps for it over the same loopback call the
browser bridge's `login()` uses to fetch a stored system's password
(`/api/internal/browser/credential` there, `/api/internal/github/credential`
here), holds it only in this process's memory for the one API call it is
about to make, and returns nothing but the pull request's number, URL and
state to the agent. **What it may change.** Opening a pull request is a write,
not a read, so it goes to the same approval broker a Bash call or a browser
click goes to when the session asks about tool calls — see `approve()` below;
verified by `test_github.py`, which asserts a denial stops the call before any
request reaches GitHub.

Deliberately importable with no network available: everything below
`GithubUnavailable` is pure logic the test suite exercises directly, and the
one call that reaches the real GitHub API (`_pull_request`) is a thin wrapper
around `urllib.request` — already used the same way by `Aiops._post` — so no
HTTP library needs to be added just for this.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("AIOPS_INTERNAL_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("AIOPS_APPROVAL_TOKEN", "")
#: ask | auto | bypass — mirrors AIOPS_BROWSER_APPROVALS exactly: the bridge
#: asks about opening a pull request exactly when a Bash call would be asked
#: about, and stays silent in the other two modes.
APPROVAL_MODE = os.environ.get("AIOPS_GITHUB_APPROVALS", "ask")
HTTP_TIMEOUT = int(os.environ.get("AIOPS_APPROVAL_HTTP_TIMEOUT", "660"))
GITHUB_API = os.environ.get("AIOPS_GITHUB_API_URL", "https://api.github.com")
PROTOCOL_VERSION = "2024-11-05"

REDACTED = "[redacted by AIOps]"


class GithubUnavailable(RuntimeError):
    pass


#: "owner/name", or a full https://github.com/owner/name(.git) URL — the exact
#: same two shapes `POST /api/workspaces/from-github` accepts, and for the
#: same reason: anything else is rejected rather than handed to GitHub's API
#: as a repository path built from whatever the model typed.
_REPO_SHORTHAND = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


def parse_repo(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    match = _REPO_URL.match(text) or _REPO_SHORTHAND.match(text)
    if match is None:
        raise ValueError(
            f"{raw!r} is not a github.com repository. Use 'owner/name' or a "
            "https://github.com/owner/name URL."
        )
    return match.group(1), match.group(2)


def redact(text, secret: str) -> str:
    """Replace the token with a marker, wherever it might appear.

    Applied to everything this process returns, the same discipline
    `mcp_browser.redact` uses — an error from a failed request could echo back
    a header or a URL the token was put in, and filtering the exit is the way
    to be sure rather than enumerating where it might leak.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if secret and len(secret) >= 6:
        text = text.replace(secret, REDACTED)
    return text


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    """One loopback call to AIOps, authenticated by this run's token."""
    body = json.dumps({**payload, "token": TOKEN}).encode()
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-AIOps-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def _detail(exc: Exception) -> str:
    body = getattr(exc, "read", None)
    if callable(body):
        try:
            detail = json.loads(body().decode() or "{}").get("detail")
            if detail:
                return str(detail)[:500]
        except Exception:  # noqa: BLE001
            pass
    return str(exc)[:500]


class Aiops:
    """The app, as this process sees it: an approver and a credential store."""

    def approve(self, tool: str, summary: str, detail: dict) -> tuple[bool, str]:
        """Put opening a pull request to the operator, if this session asks.

        Silent in auto and bypass mode — the same rule a Bash call and a
        browser click both follow, and for the same reason: this tool is
        pre-allowed at the CLI (see providers/claude.py's GITHUB_TOOLS) so that
        it still works in the two modes that give the CLI no prompt tool at
        all, and the gating that matters happens here instead.
        """
        if APPROVAL_MODE != "ask" or not TOKEN:
            return True, ""
        try:
            answer = _post(
                "/api/internal/approvals",
                {
                    "provider": "claude",
                    "kind": "tool",
                    "tool_name": tool,
                    "summary": summary,
                    "input": detail,
                },
                timeout=HTTP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - a denial is the safe outcome
            return False, f"AIOps could not be reached for approval: {exc}"
        return bool(answer.get("allowed")), str(answer.get("note") or "")

    def token(self) -> str:
        """The workspace's linked GitHub token, fetched fresh and never cached.

        Scoped entirely by the app: it resolves this run back to whoever asked
        for the turn and to the workspace their session points at, and applies
        `github_account_level_for` exactly as every other reader of a stored
        GitHub account does. This process holds the result only for the one
        API call about to be made with it.
        """
        if not TOKEN:
            raise GithubUnavailable("this run has no AIOps token, so no GitHub account can be used")
        try:
            answer = _post("/api/internal/github/credential", {}, timeout=30)
        except urllib.error.HTTPError as exc:
            raise GithubUnavailable(_detail(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise GithubUnavailable(f"AIOps could not be reached: {exc}") from exc
        secret = str(answer.get("token") or "")
        if not secret:
            raise GithubUnavailable("AIOps returned no GitHub token for this run")
        return secret


def _pull_request(token: str, owner: str, repo: str, title: str, body: str, head: str, base: str) -> dict:
    """The one call to the real GitHub API this bridge ever makes."""
    payload = json.dumps({"title": title, "body": body, "head": head, "base": base}).encode()
    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            # GitHub's API refuses a request with no User-Agent at all.
            "User-Agent": "aiops-github-bridge",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise GithubUnavailable(f"GitHub API said {exc.code}: {_detail(exc)}") from exc
    except urllib.error.URLError as exc:
        raise GithubUnavailable(f"could not reach the GitHub API: {exc.reason}") from exc


async def create_pull_request(aiops: Aiops, args: dict, holder: dict) -> str:
    """`holder["token"]` is set the moment a token is fetched, so the caller
    can redact *any* exception this raises — not just the return value — the
    same way `Browser.secrets` lets `mcp_browser.py`'s `clean()` scrub a
    traceback that happens to quote an injected password.
    """
    repo_raw = str(args.get("repo") or "")
    title = str(args.get("title") or "")
    body = str(args.get("body") or "")
    head = str(args.get("head") or "")
    base = str(args.get("base") or "")
    if not title or not head or not base:
        raise ValueError("repo, title, head and base are all required")
    owner, repo = parse_repo(repo_raw)

    ok, note = aiops.approve(
        "create_pull_request",
        f"Open a pull request on {owner}/{repo}: {head} → {base}",
        {"repo": f"{owner}/{repo}", "title": title, "head": head, "base": base},
    )
    if not ok:
        raise PermissionError(note or "The operator denied this.")

    token = await asyncio.to_thread(aiops.token)
    holder["token"] = token
    result = await asyncio.to_thread(_pull_request, token, owner, repo, title, body, head, base)

    number = result.get("number")
    url = result.get("html_url")
    state = result.get("state")
    return (
        f"Opened pull request #{number}: {url} (state: {state})"
        if number
        else f"GitHub did not return a pull request number. Raw response: {result}"
    )


TOOLS = [
    {
        "name": "create_pull_request",
        "description": (
            "Open a pull request on GitHub, using the GitHub account linked to this "
            "workspace. `repo` is 'owner/name' or a https://github.com/owner/name URL. "
            "`head` is the branch with your changes, `base` is the branch you want to "
            "merge into. The account's token is never given to you; only the pull "
            "request's number, URL and state are returned. This is a write and may be "
            "put to the operator for approval, exactly like a Bash command."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "head": {"type": "string"},
                "base": {"type": "string"},
            },
            "required": ["repo", "title", "head", "base"],
        },
    },
]


class Server:
    def __init__(self) -> None:
        self.aiops = Aiops()
        self._out = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self._out:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    async def reply(self, request_id, result: dict) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def call(self, request_id, params: dict) -> None:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        holder: dict = {}
        try:
            if name != "create_pull_request":
                raise ValueError(f"no such tool: {name}")
            text = await create_pull_request(
                self.aiops, args if isinstance(args, dict) else {}, holder
            )
            error = False
        except PermissionError as exc:
            text, error = f"Denied: {exc}", True
        except GithubUnavailable as exc:
            text, error = str(exc), True
        except Exception as exc:  # noqa: BLE001 - a tool error is an answer, not a crash
            text, error = f"{type(exc).__name__}: {exc}", True
        # Even a traceback goes through the filter — the same discipline
        # mcp_browser.py's Server.call applies, and for the same reason: a
        # fetched-but-unused token must not be recoverable from an error
        # message either.
        await self.reply(
            request_id,
            {
                "content": [{"type": "text", "text": redact(text, holder.get("token", ""))}],
                "isError": error,
            },
        )

    async def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return
        if method == "initialize":
            await self.reply(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aiops-github", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            await self.reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            await self.call(request_id, message.get("params") or {})
        elif method == "ping":
            await self.reply(request_id, {})
        else:
            await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


async def serve() -> None:
    server = Server()
    loop = asyncio.get_running_loop()
    in_flight: set[asyncio.Task] = set()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            task = asyncio.create_task(server.handle(message))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
    finally:
        if in_flight:
            await asyncio.wait(in_flight, timeout=HTTP_TIMEOUT + 5)
            for task in list(in_flight):
                task.cancel()


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(serve())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
