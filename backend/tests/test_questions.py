"""Answering the agent's questions, rather than permitting it.

Claude's `AskUserQuestion` reaches AIOps down the permission-prompt pipe like
any other tool, and the shape of the *reply* is the whole feature — so the parts
that matter are the ones a wrong guess would break silently:

* the recorded payload from a real production approval parses into the
  questions a person can actually answer;
* an answer set that is short, over-long, or invented is refused rather than
  half-sent;
* the JSON the bridge finally writes is exactly what the CLI was observed to
  accept.

That last shape was established empirically against Claude Code 2.1.x by driving
a real run through the real bridge against a stub approvals endpoint:

    {"behavior": "allow",
     "updatedInput": {<the whole original input>, "answers": {<question>: <label>}}}

    -> tool result: 'The user answered: "<question>"="<label>". Read the answers
       carefully …' and the turn continues with those answers.

A `multiSelect` question takes a list of labels (rendered comma-joined); free
text outside the offered options is passed through verbatim; an `allow` with no
`answers` yields "The user did not answer the questions.", which is the stall
this feature exists to remove.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-questions.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")

for _stale in ("./test-questions.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

from app.approvals import ApprovalBroker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Run, Session  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402
from app.providers.codex import CodexProvider  # noqa: E402
from app.questions import (  # noqa: E402
    Answer,
    AnswerError,
    build_updated_input,
    is_question_tool,
    parse_questions,
    summarise,
    validate_answers,
)
from app.schemas import ApprovalDecision, ApprovalOut  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def raises(fn, fragment=""):
    """True when `fn` refuses, for the stated reason."""
    try:
        fn()
    except AnswerError as exc:
        return fragment.lower() in str(exc).lower(), str(exc)
    return False, "did not raise"


# --- the payload production actually produced ------------------------------
# Copied from approval 82 on the live instance (run 96), which is what started
# all this: two questions, each with a header, several options, and the real
# information carried in the option *descriptions* rather than the labels.
REAL = {
    "questions": [
        {
            "question": "Does your TV or receiver handle HDR tone mapping well?",
            "header": "Device HDR",
            "multiSelect": False,
            "options": [
                {
                    "label": "Yes, keep HDR",
                    "description": "Pass HDR through untouched; the display tone maps it.",
                },
                {
                    "label": "No, tone map to SDR",
                    "description": "Convert HDR to SDR during transcode so it is not washed out.",
                },
                {
                    "label": "Only for 4K",
                    "description": "Keep HDR on 4K sources and convert everything else.",
                },
            ],
        },
        {
            "question": "Which quality profiles should Radarr and Sonarr prefer?",
            "header": "Profiles",
            "multiSelect": True,
            "options": [
                {"label": "Remux", "description": "Untouched disc rip; largest files."},
                {"label": "Bluray-1080p", "description": "Encoded 1080p; a good default."},
                {"label": "WEBDL-2160p", "description": "Streaming-sourced 4K."},
                {"label": "HDTV", "description": "Broadcast capture; smallest and worst."},
            ],
        },
    ]
}

parsed = parse_questions(REAL)
check("the recorded production payload parses", len(parsed) == 2, str(len(parsed)))
check(
    "each question keeps its header, its wording and its multi-select flag",
    parsed[0].header == "Device HDR"
    and parsed[0].multi_select is False
    and parsed[1].header == "Profiles"
    and parsed[1].multi_select is True,
    f"{parsed[0].header!r}/{parsed[0].multi_select} {parsed[1].header!r}/{parsed[1].multi_select}",
)
check(
    "every option keeps its description — that is where the decision is",
    [o.description for o in parsed[0].options]
    == [
        "Pass HDR through untouched; the display tone maps it.",
        "Convert HDR to SDR during transcode so it is not washed out.",
        "Keep HDR on 4K sources and convert everything else.",
    ],
    str([o.description for o in parsed[0].options]),
)
check(
    "the summary is the question, not the tool's name",
    summarise(parsed) == "Does your TV or receiver handle HDR tone mapping well? (+1 more)",
    repr(summarise(parsed)),
)
check(
    "a long question is cut rather than filling a notification",
    len(summarise(parse_questions({"questions": [{"question": "q " * 400, "options": []}]}))) <= 160,
)

check(
    "it is only this tool, on the provider that has it",
    is_question_tool("claude", "AskUserQuestion")
    and not is_question_tool("codex", "AskUserQuestion")
    and not is_question_tool("claude", "AskUserQuestionOfMine")
    and not is_question_tool("claude", "Bash"),
)

# Anything unreadable degrades to "not a question", so the ordinary tool card is
# shown rather than an exception thrown mid-turn.
for label, payload in [
    ("a plain tool input", {"command": "ls"}),
    ("questions that are not a list", {"questions": "pick one"}),
    ("a question with no text", {"questions": [{"header": "h", "options": []}]}),
    ("nothing at all", None),
    ("two questions with the same wording", {"questions": [{"question": "a"}, {"question": "a"}]}),
]:
    check(f"{label} yields no questions", parse_questions(payload) == [], repr(payload)[:80])

check(
    "a question with no options still parses — the other box can answer it",
    len(parse_questions({"questions": [{"question": "Which host?"}]})) == 1,
)


# --- validation ------------------------------------------------------------
hdr, profiles = parsed[0].question, parsed[1].question

good = [
    Answer(question=hdr, options=("Only for 4K",)),
    Answer(question=profiles, options=("Remux", "WEBDL-2160p")),
]
built = validate_answers(parsed, good)

check(
    "single-select answers as a string, multi-select as a list",
    built == {hdr: "Only for 4K", profiles: ["Remux", "WEBDL-2160p"]},
    json.dumps(built),
)

free = validate_answers(
    parsed,
    [
        Answer(question=hdr, text="  Only on the projector, not the TV  "),
        Answer(question=profiles, options=("Bluray-1080p",), text="and Remux when it is under 30GB"),
    ],
)
check(
    "free text answers a question in the person's own words, trimmed",
    free[hdr] == "Only on the projector, not the TV",
    repr(free[hdr]),
)
check(
    "a multi-select question takes options and free text together",
    free[profiles] == ["Bluray-1080p", "and Remux when it is under 30GB"],
    json.dumps(free[profiles]),
)

ok, why = raises(
    lambda: validate_answers(parsed, [Answer(question=hdr, options=("Only for 4K",))]),
    "still unanswered",
)
check("an unanswered question is refused, not sent half-finished", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=()),
            Answer(question=profiles, options=("Remux",)),
        ],
    ),
    "still unanswered",
)
check("a question answered with nothing is refused", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=("Yes, keep HDR", "Only for 4K")),
            Answer(question=profiles, options=("Remux",)),
        ],
    ),
    "takes one answer",
)
check("a single-select question refuses two answers", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=("Yes, keep HDR",), text="or maybe not"),
            Answer(question=profiles, options=("Remux",)),
        ],
    ),
    "takes one answer",
)
check("single-select counts the other box as an answer too", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=("Dolby Vision only",)),
            Answer(question=profiles, options=("Remux",)),
        ],
    ),
    "was not offered",
)
check("an option that was never offered is refused", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=("Only for 4K",)),
            Answer(question=profiles, options=("Remux", "Remux")),
        ],
    ),
    "twice",
)
check("the same option twice is refused", ok, why)

ok, why = raises(
    lambda: validate_answers(
        parsed,
        [
            Answer(question=hdr, options=("Only for 4K",)),
            Answer(question=profiles, options=("Remux",)),
            Answer(question="Which colour?", options=("Red",)),
        ],
    ),
    "not a question that was asked",
)
check("an answer to a question nobody asked is refused", ok, why)

ok, why = raises(lambda: validate_answers([], [Answer(question=hdr)]), "no questions")
check("answering an approval with no questions is refused", ok, why)

check(
    "the reply carries the whole original input, with answers added",
    build_updated_input(REAL, built) == {**REAL, "answers": built},
    "shape differs",
)
check(
    "the original input is not mutated in the process",
    "answers" not in REAL,
)


# --- how it is exposed to the browser --------------------------------------
class Row:
    """Enough of an Approval row for the response model."""

    def __init__(self, **kw):
        self.id = 1
        self.run_id = 1
        self.session_id = "s"
        self.provider = "claude"
        self.kind = "question"
        self.tool_name = "AskUserQuestion"
        self.summary = "x"
        self.request = REAL
        self.status = "pending"
        self.decided_by_id = None
        self.decided_at = None
        self.note = None
        self.created_at = __import__("datetime").datetime.now()
        self.__dict__.update(kw)


out = ApprovalOut.model_validate(Row())
check(
    "the API hands the browser the questions, already parsed",
    len(out.questions) == 2
    and out.questions[0].header == "Device HDR"
    and out.questions[1].multi_select is True
    and out.questions[0].options[0].description.startswith("Pass HDR"),
    out.model_dump_json()[:160],
)

plain = ApprovalOut.model_validate(Row(tool_name="Bash", kind="tool", request={"command": "ls"}))
check(
    "an ordinary tool approval is unchanged, and carries no questions",
    plain.questions == [] and plain.request == {"command": "ls"} and plain.kind == "tool",
    plain.model_dump_json()[:160],
)
check(
    "a Codex tool of the same name is not treated as a question",
    ApprovalOut.model_validate(Row(provider="codex")).questions == [],
)
check(
    "the decision schema still accepts a bare allow/deny",
    ApprovalDecision.model_validate({"allowed": True}).answers is None
    and ApprovalDecision.model_validate({"allowed": False, "note": "no"}).note == "no",
)


# --- the broker: kind, and how long a person gets --------------------------
async def broker_tests():
    await init_db()
    async with SessionLocal() as db:
        sess = Session(provider="claude", title="questions")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        run = Run(session_id=sess.id, prompt="p", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        session_id, run_id = sess.id, run.id

    broker = ApprovalBroker()

    # An unanswered question expires like anything else — it must not hang the
    # turn — but it is told it went unanswered rather than that it was refused,
    # because nobody refused it.
    started = time.monotonic()
    timed = await broker.request(
        run_id=run_id, session_id=session_id, provider="claude", kind="question",
        tool_name="AskUserQuestion", summary="q", request=REAL, timeout=0.3,
    )
    check("an unanswered question still releases the agent", timed.allowed is False)
    check(
        "and is described as unanswered, not as a denial",
        "nobody answered" in (timed.note or "").lower()
        and "denied" not in (timed.note or "").lower(),
        repr(timed.note),
    )
    check(
        "the wait is quoted in minutes a person would recognise",
        "minute" in (timed.note or "") or "second" in (timed.note or ""),
        repr(timed.note),
    )
    check("it really waited", time.monotonic() - started >= 0.3)

    # The wording for an ordinary tool approval is untouched.
    plain_timeout = await broker.request(
        run_id=run_id, session_id=session_id, provider="claude", kind="tool",
        tool_name="Bash", summary="b", request={"command": "ls"}, timeout=0.3,
    )
    check(
        "an ordinary approval still times out saying it was denied",
        plain_timeout.allowed is False and "denied" in (plain_timeout.note or "").lower(),
        repr(plain_timeout.note),
    )

    # Answers ride out on the decision, which is what the bridge turns into
    # `updatedInput`.
    waiter = asyncio.create_task(
        broker.request(
            run_id=run_id, session_id=session_id, provider="claude", kind="question",
            tool_name="AskUserQuestion", summary="q", request=REAL, timeout=10,
        )
    )
    await asyncio.sleep(0.2)
    from sqlalchemy import select

    from app.models import Approval

    async with SessionLocal() as db:
        pending = list(await db.scalars(select(Approval).where(Approval.status == "pending")))
    answered = await broker.decide(
        pending[0].id,
        allowed=True,
        note=None,
        user_id=None,
        updated_input=build_updated_input(REAL, built),
    )
    decision = await waiter
    check("the answers reach the waiting agent", answered and decision.allowed)
    check(
        "as the whole input plus the answers object",
        decision.updated_input == {**REAL, "answers": built},
        json.dumps(decision.updated_input)[:120],
    )


asyncio.run(broker_tests())

check(
    "questions get a longer wait than an allow/deny tap",
    settings.approval_question_timeout_seconds > settings.approval_timeout_seconds,
    f"{settings.approval_question_timeout_seconds}s vs {settings.approval_timeout_seconds}s",
)

# The bridge parks on an HTTP request for the whole wait. If it gives up first,
# a socket decides the question instead of a person.
spec = ClaudeProvider().build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="ask", approval_token="tok",
)
check(
    "the bridge is told to outlast the longest wait the server will take",
    int(spec.env["AIOPS_APPROVAL_HTTP_TIMEOUT"]) > settings.approval_question_timeout_seconds,
    spec.env.get("AIOPS_APPROVAL_HTTP_TIMEOUT", "unset"),
)

# Codex has no AskUserQuestion. Nothing here should have reached it.
cx = CodexProvider().build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="ask", approval_token="tok",
)
check(
    "codex is untouched by any of this",
    "AIOPS_APPROVAL_HTTP_TIMEOUT" not in cx.env and "AskUserQuestion" not in " ".join(cx.argv),
    str(sorted(cx.env)),
)


# --- the wire shape the CLI actually validates -----------------------------
class StubAIOps(BaseHTTPRequestHandler):
    decision = {"allowed": True, "note": None, "updated_input": None}
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        StubAIOps.seen.append(json.loads(body))
        payload = json.dumps(StubAIOps.decision).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), StubAIOps)
threading.Thread(target=server.serve_forever, daemon=True).start()
stub_url = f"http://127.0.0.1:{server.server_port}"
BRIDGE = os.path.join("app", "bridge", "mcp_approver.py")


def drive_bridge(messages, timeout=20):
    proc = subprocess.run(
        [sys.executable, BRIDGE],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            **os.environ,
            "AIOPS_INTERNAL_URL": stub_url,
            "AIOPS_APPROVAL_TOKEN": "tok-q",
            "AIOPS_PROVIDER": "claude",
        },
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


StubAIOps.seen.clear()
StubAIOps.decision = {
    "allowed": True,
    "note": None,
    "updated_input": build_updated_input(REAL, built),
}
out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "ask",
        "arguments": {"tool_name": "AskUserQuestion", "input": REAL, "tool_use_id": "toolu_q"},
    }},
])
reply = json.loads([m for m in out if m.get("id") == 7][0]["result"]["content"][0]["text"])
check(
    "the bridge writes exactly the reply the CLI was observed to accept",
    reply == {"behavior": "allow", "updatedInput": {**REAL, "answers": built}},
    json.dumps(reply)[:200],
)
check(
    "the whole question set reaches AIOps, so the server can read it",
    StubAIOps.seen and StubAIOps.seen[0]["input"] == REAL,
    json.dumps(StubAIOps.seen[:1])[:160],
)

# Declining is an ordinary deny, and the CLI hands the message to the model.
StubAIOps.decision = {
    "allowed": False,
    "note": "Nobody answered these questions.",
    "updated_input": None,
}
out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
        "name": "ask", "arguments": {"tool_name": "AskUserQuestion", "input": REAL},
    }},
])
declined = json.loads([m for m in out if m.get("id") == 8][0]["result"]["content"][0]["text"])
check(
    "declining reaches the agent as a deny carrying the reason",
    declined == {"behavior": "deny", "message": "Nobody answered these questions."},
    json.dumps(declined),
)

# Regression: an ordinary tool approval still produces the bare allow, with no
# updatedInput invented for it.
StubAIOps.decision = {"allowed": True, "note": None, "updated_input": None}
out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
        "name": "ask", "arguments": {"tool_name": "Bash", "input": {"command": "ls"}},
    }},
])
plain_reply = json.loads([m for m in out if m.get("id") == 9][0]["result"]["content"][0]["text"])
check(
    "an ordinary allow is still the bare allow it always was",
    plain_reply == {"behavior": "allow"},
    json.dumps(plain_reply),
)

server.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
