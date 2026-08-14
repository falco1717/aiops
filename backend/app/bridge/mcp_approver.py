#!/usr/bin/env python3
"""A minimal stdio MCP server that turns Claude's permission prompts into
AIOps approval requests.

Claude Code is started with::

    --mcp-config '{"mcpServers":{"aiops":{"command":"...","args":["..."]}}}'
    --permission-prompt-tool mcp__aiops__ask

and then calls the ``ask`` tool before running any tool it is not already
allowed to use. The call blocks until this script answers, which is what lets a
human decide from the web UI while the agent genuinely waits.

The contract Claude enforces on the reply is::

    {"behavior": "allow", "updatedInput"?: object}
    {"behavior": "deny",  "message": string}

returned JSON-encoded inside an ordinary text content block.

This runs as a subprocess of the agent, so it has no user session. It
authenticates back to AIOps with a per-run token supplied in its environment.

Deliberately dependency-free: it is spawned by the CLI, not by our app, so it
must not assume the venv's packages are importable.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("AIOPS_INTERNAL_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("AIOPS_APPROVAL_TOKEN", "")
PROVIDER = os.environ.get("AIOPS_PROVIDER", "claude")
# Slightly longer than the server's own wait, so the server is what times out
# and records the decision, not us.
HTTP_TIMEOUT = int(os.environ.get("AIOPS_APPROVAL_HTTP_TIMEOUT", "660"))

PROTOCOL_VERSION = "2024-11-05"

_write_lock = threading.Lock()


def _send(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _reply(request_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _summarise(tool_name: str, tool_input: dict) -> str:
    """One line a human can judge without expanding the raw JSON."""
    if not isinstance(tool_input, dict):
        return tool_name
    for key in ("command", "file_path", "path", "url", "pattern", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {value.strip()}"
    if tool_name == "Task" and isinstance(tool_input.get("description"), str):
        return f"{tool_name}: {tool_input['description']}"
    return tool_name


def _decide(tool_name: str, tool_input: dict, tool_use_id: str | None) -> dict:
    """Ask AIOps. Any failure denies — never fail open."""
    payload = json.dumps(
        {
            "token": TOKEN,
            "provider": PROVIDER,
            "kind": "tool",
            "tool_name": tool_name,
            "summary": _summarise(tool_name, tool_input),
            "input": tool_input if isinstance(tool_input, dict) else {},
            "tool_use_id": tool_use_id,
        }
    ).encode()
    request = urllib.request.Request(
        f"{API_URL}/api/internal/approvals",
        data=payload,
        headers={"Content-Type": "application/json", "X-AIOps-Token": TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            answer = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return {"behavior": "deny", "message": f"AIOps rejected the approval request ({exc.code})."}
    except Exception as exc:  # noqa: BLE001 - a denial is the safe outcome
        return {"behavior": "deny", "message": f"AIOps could not be reached for approval: {exc}"}

    if answer.get("allowed"):
        allow: dict = {"behavior": "allow"}
        updated = answer.get("updated_input")
        if isinstance(updated, dict) and updated:
            allow["updatedInput"] = updated
        return allow
    return {"behavior": "deny", "message": answer.get("note") or "Denied in AIOps."}


TOOLS = [
    {
        "name": "ask",
        "description": (
            "Ask the AIOps operator to approve or deny a tool call. Blocks until "
            "a human answers in the web UI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
                "tool_use_id": {"type": "string"},
            },
            "required": ["tool_name", "input"],
        },
    }
]


def _handle_call(request_id, params: dict) -> None:
    args = params.get("arguments") or {}
    decision = _decide(
        str(args.get("tool_name") or "unknown"),
        args.get("input") or {},
        args.get("tool_use_id"),
    )
    # The decision travels as JSON *text*, not as structured content.
    _reply(request_id, {"content": [{"type": "text", "text": json.dumps(decision)}]})


def main() -> None:
    # Calls are answered on their own threads so a human deliberating over one
    # request does not block another. They must be joined before returning:
    # when stdin closes, the interpreter would otherwise tear the threads down
    # mid-flight and the reply would never be written.
    in_flight: list[threading.Thread] = []

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        request_id = message.get("id")

        # Notifications carry no id and must not be answered.
        if request_id is None:
            continue

        if method == "initialize":
            _reply(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aiops-approvals", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            _reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            worker = threading.Thread(
                target=_handle_call,
                args=(request_id, message.get("params") or {}),
                daemon=True,
            )
            worker.start()
            in_flight.append(worker)
            in_flight = [t for t in in_flight if t.is_alive()]
        elif method in ("ping",):
            _reply(request_id, {})
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

    # Bounded, so a wedged request cannot keep this process alive forever.
    deadline = time.monotonic() + HTTP_TIMEOUT + 5
    for worker in in_flight:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
