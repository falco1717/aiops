"""Stands in for the real `claude` CLI, emitting the documented stream-json shape.

Used by test_runner.py so the runner and parser can be exercised without a
signed-in CLI. It sleeps between deltas so "session is busy" states are
deterministic rather than racy.
"""
import json
import sys
import time

args = sys.argv[1:]


def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default


session_id = flag("--session-id") or flag("--resume") or "fake-session-0001"
model = flag("--model", "opus")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit({
    "type": "system",
    "subtype": "init",
    "session_id": session_id,
    "model": model,
    "tools": ["Read", "Edit", "Bash"],
    "mcp_servers": [],
})

# Token-level deltas: streamed live over the websocket, never persisted.
for chunk in ("Check", "ing the ", "README"):
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": chunk}}})
    time.sleep(0.35)

emit({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "The file is short, I can read it directly."}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "README.md"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "# Demo\nA sample project."}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Checking the README — it describes a sample project."}]}})
emit({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "The README describes a sample project.",
    "session_id": session_id,
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 1200, "output_tokens": 90},
})
sys.exit(0)
