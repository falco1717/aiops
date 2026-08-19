"""The pull-request MCP bridge: approval gating, and that the token never
comes back out.

Written as the mirror of the "tools, approvals and redaction" section of
`test_browser.py`: the loopback API is stood in for with a fake that records
every call, and the property being pinned is that a denied approval never
reaches the network at all, and that the token — fetched fresh for every call
and never cached — cannot be recovered from any tool response, including an
error one.

Deliberately importable with no network reachable: `mcp_github.py` is written
so that everything except the one real GitHub API call is pure logic (see its
own module docstring), and this suite never calls `_pull_request` for real —
it patches it out, the same way `test_browser.py` never launches a real
Chromium.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


from app.bridge import mcp_github as gh  # noqa: E402

TOKEN = "ghp_" + "z" * 36


class Calls:
    """Stands in for the loopback API, recording everything asked of it."""

    def __init__(self, allow=True, token=TOKEN):
        self.allow = allow
        self.token = token
        self.posts = []

    def __call__(self, path, payload, timeout=30):
        self.posts.append((path, dict(payload)))
        if path.endswith("/approvals"):
            return {"allowed": self.allow, "note": None if self.allow else "Not this one."}
        if path.endswith("/github/credential"):
            return {"token": self.token} if self.token else {}
        return {"ok": True}


def fresh(allow=True, mode="ask", token=TOKEN):
    gh.APPROVAL_MODE = mode
    gh.TOKEN = "run-token"
    calls = Calls(allow=allow, token=token)
    gh._post = calls
    return gh.Aiops(), calls


# -- parse_repo: the same two shapes the clone endpoint accepts ---------
check("owner/name is accepted", gh.parse_repo("octocat/Hello-World") == ("octocat", "Hello-World"))
check("a full github.com URL is accepted",
      gh.parse_repo("https://github.com/octocat/Hello-World") == ("octocat", "Hello-World"))
check("a trailing .git is stripped",
      gh.parse_repo("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World"))
for bad in ("git@github.com:octocat/x.git", "https://gitlab.com/o/n", "not a repo", ""):
    try:
        gh.parse_repo(bad)
        check(f"{bad!r} is rejected", False, "did not raise")
    except ValueError:
        check(f"{bad!r} is rejected", True)

# -- redact: the same pure function the browser bridge relies on --------
check("a token is replaced wherever it appears",
      gh.redact(f"failed: {TOKEN} was invalid", TOKEN) == f"failed: {gh.REDACTED} was invalid")
check("every occurrence, not only the first",
      gh.redact(f"{TOKEN} {TOKEN}", TOKEN).count(TOKEN) == 0)
check("a very short value is left alone", gh.redact("abc", "ab") == "abc")
check("nothing at all is a no-op", gh.redact("plain", "") == "plain")


async def main():
    server = gh.Server()

    # -- denied: nothing reaches GitHub at all ---------------------------
    aiops, calls = fresh(allow=False, mode="ask")
    server.aiops = aiops
    called = {"hit": False}

    def _boom(*a, **kw):
        called["hit"] = True
        raise AssertionError("the real GitHub call must never run when denied")

    gh._pull_request = _boom
    reply = await _call(server, "create_pull_request", {
        "repo": "octocat/Hello-World", "title": "t", "head": "feature", "base": "main",
    })
    check("a denied pull request is refused", reply["isError"] is True, str(reply))
    check("and says so", "Denied" in reply["content"][0]["text"], reply["content"][0]["text"])
    check("the GitHub API was never called", not called["hit"])
    check("and no token was ever fetched for it either",
          not any(p.endswith("/github/credential") for p, _ in calls.posts), str(calls.posts))

    # -- auto/bypass: no approval call at all, same as the browser bridge -
    for mode in ("auto", "bypass"):
        aiops, calls = fresh(allow=True, mode=mode)
        server.aiops = aiops
        gh._pull_request = lambda *a, **kw: {
            "number": 7, "html_url": "https://github.com/o/n/pull/7", "state": "open",
        }
        reply = await _call(server, "create_pull_request", {
            "repo": "octocat/Hello-World", "title": "t", "head": "feature", "base": "main",
        })
        check(f"in {mode} mode the call proceeds without asking",
              not any(p.endswith("/approvals") for p, _ in calls.posts), str(calls.posts))
        check(f"and still succeeds in {mode} mode",
              reply["isError"] is False and "#7" in reply["content"][0]["text"],
              str(reply))

    # -- the property this whole file exists to check --------------------
    aiops, calls = fresh(allow=True, mode="ask")
    server.aiops = aiops
    gh._pull_request = lambda *a, **kw: {
        "number": 42, "html_url": "https://github.com/octocat/Hello-World/pull/42",
        "state": "open",
    }
    reply = await _call(server, "create_pull_request", {
        "repo": "octocat/Hello-World", "title": "t", "head": "feature", "base": "main",
    })
    check("a successful call approves first", any(p.endswith("/approvals") for p, _ in calls.posts))
    check("then fetches the token", any(p.endswith("/github/credential") for p, _ in calls.posts))
    check("and the reply carries only the PR result",
          reply["isError"] is False and "#42" in reply["content"][0]["text"], str(reply))
    check("the token is never in the reply", TOKEN not in reply["content"][0]["text"])
    import json as _json
    check("nor anywhere in the reply once serialised to JSON",
          TOKEN not in _json.dumps(reply))

    # -- even a failure that echoes the token is redacted -----------------
    aiops, calls = fresh(allow=True, mode="ask")
    server.aiops = aiops

    def _leaky(*a, **kw):
        raise RuntimeError(f"GitHub API said 401: bad credentials {TOKEN}")

    gh._pull_request = _leaky
    reply = await _call(server, "create_pull_request", {
        "repo": "octocat/Hello-World", "title": "t", "head": "feature", "base": "main",
    })
    check("a failure that echoes the token back is still redacted",
          TOKEN not in reply["content"][0]["text"] and gh.REDACTED in reply["content"][0]["text"],
          reply["content"][0]["text"])
    check("and it is reported as an error, not swallowed", reply["isError"] is True)

    # -- no account linked: the credential fetch fails, no token, no crash
    aiops, calls = fresh(allow=True, mode="ask", token="")
    server.aiops = aiops
    gh._pull_request = lambda *a, **kw: {"number": 1, "html_url": "x", "state": "open"}
    reply = await _call(server, "create_pull_request", {
        "repo": "octocat/Hello-World", "title": "t", "head": "feature", "base": "main",
    })
    check("no linked account is a clean error, not a crash", reply["isError"] is True)
    check("and it does not claim a PR was opened",
          "Opened pull request" not in reply["content"][0]["text"], reply["content"][0]["text"])

    # -- an unknown tool name is an error, not a crash --------------------
    reply = await _call(server, "not_a_real_tool", {})
    check("an unknown tool name is a clean error", reply["isError"] is True)

    # -- required arguments are enforced before anything is fetched -------
    aiops, calls = fresh(allow=True, mode="ask")
    server.aiops = aiops
    reply = await _call(server, "create_pull_request", {"repo": "octocat/Hello-World"})
    check("missing title/head/base is refused", reply["isError"] is True)
    check("before any approval was even asked for",
          not any(p.endswith("/approvals") for p, _ in calls.posts), str(calls.posts))

    # -- tools/list answers without touching the network -----------------
    listed = {}

    async def capture_reply(request_id, result):
        listed["result"] = result

    server.reply = capture_reply
    await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    check("tools/list advertises create_pull_request", names == ["create_pull_request"], str(names))


async def _call(server, name, args) -> dict:
    """Drive `Server.call` and capture what it would have sent over stdio."""
    captured = {}

    async def capture(request_id, result):
        captured.update(result)

    server.reply = capture
    await server.call(1, {"name": name, "arguments": args})
    return captured


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
