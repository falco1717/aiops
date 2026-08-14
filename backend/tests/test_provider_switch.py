"""Moving a session from Claude to Codex, and back, without lying about it.

A switch is not a state transfer and cannot be made into one: each CLI resumes
only sessions it created itself, so the incoming agent necessarily starts a fresh
conversation. What AIOps can do is write it a briefing out of the transcript it
kept. That makes three things worth testing that a "does the dropdown work"
suite would miss:

* the briefing must fire **exactly once**. Firing on every turn afterwards wastes
  the context window and tells the agent it is new when it is not; firing never
  leaves it answering a question with no idea what was agreed.
* `run.prompt` must stay exactly what the operator typed while the CLI receives
  the prompt *plus* the briefing. They are the same string everywhere else in the
  UI, so a briefing leaking into the transcript would be invisible in review and
  permanent in the record.
* the briefing must not replay a secret. The incident behind that requirement was
  an agent dumping `AIOPS_SECRET_KEY` into a tool result, which is now sitting in
  a transcript that this feature reads back and sends to another agent.

The prompt actually handed to each CLI is captured at `build_run`, which is the
same seam test_runner.py uses — so what is asserted below is the argv the runner
really built, not a re-derivation of it.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-provider-switch.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
# Codex in "ask" mode is driven over the app-server's JSON-RPC protocol instead
# of as a subprocess, and that adapter is covered by its own suite. Everything
# here is about the prompt a CLI is handed, so both providers stay on the
# subprocess path.
os.environ.setdefault("AIOPS_DEFAULT_APPROVAL_MODE", "auto")
#: A real-looking key, so the literal-masking pass in redaction.py has something
#: to find. It never encrypts anything here. Forced rather than defaulted: the
#: point of the check below is that *this process's own* key cannot be replayed
#: into another agent's prompt, so the value redaction sees has to be this one.
SECRET = "c0FUcnVseVNlY3JldFRlc3RLZXlfMDEyMzQ1Njc4OT0="
os.environ["AIOPS_SECRET_KEY"] = SECRET
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-switch-test-ws-")
)
os.environ.setdefault(
    "AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-switch-test-att-")
)

DB_FILE = os.environ["AIOPS_DATABASE_URL"].split("///", 1)[-1]
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CLAUDE = os.path.join(HERE, "fake_claude_cli.py")

#: A stand-in Codex CLI, in the shape `codex exec --json` really emits. Inline
#: rather than a file of its own because it needs to be nothing but a mouth: the
#: Codex parser has a suite already, and what matters here is that a turn on the
#: other provider completes and its prompt can be read back.
FAKE_CODEX = (
    "import json,sys\n"
    "e=lambda o: (sys.stdout.write(json.dumps(o)+chr(10)), sys.stdout.flush())\n"
    "e({'type':'thread.started','thread_id':'codex-thread-0001'})\n"
    "e({'type':'item.completed','item':{'type':'agent_message',"
    "'text':'Picked up from the briefing; continuing the billing migration.'}})\n"
    "e({'type':'turn.completed','usage':{'input_tokens':400,'output_tokens':20}})\n"
)

from app.providers import PROVIDERS  # noqa: E402
from app.providers.base import RunSpec  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402
from app.providers.codex import CodexProvider  # noqa: E402

#: (provider, prompt) for every turn, in order, exactly as the CLI received it.
sent: list[tuple[str, str]] = []

_claude_build = ClaudeProvider.build_run
_codex_build = CodexProvider.build_run


def claude_patched(self, **kwargs):
    sent.append(("claude", kwargs["prompt"]))
    spec = _claude_build(self, **kwargs)
    return RunSpec(
        argv=[sys.executable, FAKE_CLAUDE, *spec.argv[1:]],
        env=spec.env,
        assigned_session_id=spec.assigned_session_id,
    )


def codex_patched(self, **kwargs):
    sent.append(("codex", kwargs["prompt"]))
    spec = _codex_build(self, **kwargs)
    return RunSpec(argv=[sys.executable, "-c", FAKE_CODEX], env=spec.env)


ClaudeProvider.build_run = claude_patched
CodexProvider.build_run = codex_patched
PROVIDERS["claude"] = ClaudeProvider()
PROVIDERS["codex"] = CodexProvider()

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from app import handoff, migrate, redaction  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Event, Run, Session  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def settle(client, sid, timeout=40):
    """Wait for every turn in this session to finish, then return them."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.get(f"/api/sessions/{sid}/runs").json()
        if runs and all(r["status"] not in ("queued", "running") for r in runs):
            return runs
        time.sleep(0.15)
    return client.get(f"/api/sessions/{sid}/runs").json()


def login(client, username, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


FIRST_TASK = "Plan the migration that splits the billing table in two."
SECOND_TASK = "Carry on from where that got to, and start with the index."
THIRD_TASK = "Now do the same for invoices."


with TestClient(app) as c:
    login(c, "admin", "devpassword123")

    # Two accounts and a preset, so "an account belongs to one provider" and
    # "a preset pins one provider's vocabulary" are real conditions rather than
    # nulls that trivially survive a switch.
    claude_account = c.post(
        "/api/accounts", json={"name": "Test Claude", "provider": "claude"}
    ).json()
    codex_account = c.post(
        "/api/accounts", json={"name": "Test Codex", "provider": "codex"}
    ).json()
    claude_preset = c.post(
        "/api/presets",
        json={"name": "Claude Ops", "provider": "claude", "model": "sonnet"},
    ).json()

    r = c.post("/api/sessions", json={
        "provider": "claude",
        "title": "Billing split",
        "model": "opus",
        "effort": "high",
        "account_id": claude_account["id"],
        "preset_id": claude_preset["id"],
        "approval_mode": "auto",
    })
    check("a claude session can be created", r.status_code == 201, r.text[:300])
    sid = r.json()["id"]

    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": FIRST_TASK})
    runs = settle(c, sid)
    check("the first turn ran", len(runs) == 1 and runs[0]["status"] == "succeeded",
          str([(r_["status"], r_["error"]) for r_ in runs])[:300])
    check("and is stamped with the provider that answered it",
          runs[0]["provider"] == "claude", str(runs[0]["provider"]))
    check("and with the model it ran on",
          runs[0]["model"] == "opus", str(runs[0]["model"]))
    check("the first turn carried no briefing — there was nothing to hand over",
          runs[0]["carries_handoff"] is False, str(runs[0]["carries_handoff"]))

    before = c.get(f"/api/sessions/{sid}").json()
    check("the claude session id was captured, so there is something to lose",
          bool(before["provider_session_id"]), str(before["provider_session_id"]))
    check("and nothing is owed to anybody yet",
          before["handoff_pending"] is False, str(before["handoff_pending"]))

    # --- who may switch it -------------------------------------------------
    # Seeing a conversation and re-pointing it at another agent are different
    # rights: a switch throws away the resumable session behind somebody else's
    # work. So the check is specifically that a sharee who *can* read it cannot
    # do this — a 404 here would prove nothing.
    dana = c.post("/api/users", json={
        "username": "dana", "password": "danapassword1",
        "is_admin": False, "must_change_password": False,
    }).json()["id"]
    c.patch(f"/api/sessions/{sid}", json={"shared_user_ids": [dana]})

    login(c, "dana", "danapassword1")
    check("a sharee really can see the session",
          c.get(f"/api/sessions/{sid}").status_code == 200)
    r = c.patch(f"/api/sessions/{sid}", json={"provider": "codex"})
    check("but cannot switch which agent runs it", r.status_code == 403,
          f"{r.status_code} {r.text[:200]}")
    check("and the refusal says why rather than 404ing",
          "owner" in r.text.lower(), r.text[:200])
    check("the session is untouched by the attempt",
          c.get(f"/api/sessions/{sid}").json()["provider"] == "claude")

    login(c, "admin", "devpassword123")

    # --- mid-turn is ambiguous, so it is refused ---------------------------
    # The stand-in CLI streams deltas with sleeps in between, which is what makes
    # "while a run is active" a real window rather than a race.
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": THIRD_TASK})
    r = c.patch(f"/api/sessions/{sid}", json={"provider": "codex"})
    check("a switch mid-turn is refused with 409", r.status_code == 409,
          f"{r.status_code} {r.text[:200]}")
    check("and explains that the turn has already gone to the other agent",
          "turn" in r.text.lower(), r.text[:200])
    settle(c, sid)
    mid = c.get(f"/api/sessions/{sid}").json()
    check("the refused switch changed nothing at all",
          mid["provider"] == "claude" and mid["provider_session_id"]
          and mid["handoff_pending"] is False,
          f"{mid['provider']} {mid['provider_session_id']} {mid['handoff_pending']}")

    # --- an incoherent switch is a 4xx, not a 500 and not a carried lie ----
    r = c.patch(f"/api/sessions/{sid}", json={"provider": "codex", "model": "opus"})
    check("switching while asking for the other provider's model is refused",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    check("and the refusal lists the models that do exist",
          "gpt-5.6-sol" in r.text, r.text[:300])

    r = c.patch(f"/api/sessions/{sid}", json={
        "provider": "codex", "account_id": claude_account["id"],
    })
    check("nor may it keep an account belonging to the provider it left",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = c.patch(f"/api/sessions/{sid}", json={"provider": "codex", "effort": "banana"})
    check("nor an effort level neither CLI has", r.status_code == 400,
          f"{r.status_code} {r.text[:200]}")

    r = c.patch(f"/api/sessions/{sid}", json={"provider": "gemini"})
    check("nor a provider that does not exist", r.status_code == 400,
          f"{r.status_code} {r.text[:200]}")

    # Every one of those was refused *after* the switch had begun rearranging
    # the row, so this is the check that the refusal is a refusal.
    intact = c.get(f"/api/sessions/{sid}").json()
    check("a refused switch leaves the session entirely as it was",
          intact["provider"] == "claude"
          and intact["provider_session_id"] == before["provider_session_id"]
          and intact["model"] == "opus"
          and intact["account_id"] == claude_account["id"]
          and intact["handoff_pending"] is False,
          json.dumps({k: intact[k] for k in
                      ("provider", "model", "account_id", "handoff_pending")}))
    events = c.get(f"/api/sessions/{sid}/transcript").json()["events"]
    check("and writes no switch into the transcript",
          not any(e["kind"] == "provider_switch" for e in events),
          str([e["kind"] for e in events]))

    # --- the switch itself -------------------------------------------------
    r = c.patch(f"/api/sessions/{sid}", json={"provider": "codex"})
    check("the owner can switch the session to codex", r.status_code == 200,
          f"{r.status_code} {r.text[:300]}")
    after = r.json() if r.status_code == 200 else {}
    check("the session now runs on codex", after.get("provider") == "codex",
          str(after.get("provider")))
    check("the old provider session id is gone — it means nothing to the new CLI",
          after.get("provider_session_id") is None,
          str(after.get("provider_session_id")))
    check("a briefing is owed to the next turn",
          after.get("handoff_pending") is True, str(after.get("handoff_pending")))
    check("the model was cleared rather than carried across as nonsense",
          after.get("model") is None, str(after.get("model")))
    check("so was the account, which belonged to the other provider",
          after.get("account_id") is None, str(after.get("account_id")))
    check("and the preset, which pinned the other provider's model",
          after.get("preset_id") is None, str(after.get("preset_id")))
    # "high" is a level Codex also has, so it is kept: the rule is "clear what
    # cannot work", not "clear everything".
    check("an effort level the new provider also accepts is kept",
          after.get("effort") == "high", str(after.get("effort")))

    transcript = c.get(f"/api/sessions/{sid}/transcript").json()
    switches = [e for e in transcript["events"] if e["kind"] == "provider_switch"]
    check("the switch is recorded in the transcript, not just in a column",
          len(switches) == 1, str([e["kind"] for e in transcript["events"]]))
    if switches:
        check("and names both agents and who did it",
              "Claude" in switches[0]["text"] and "Codex" in switches[0]["text"]
              and "admin" in switches[0]["text"], switches[0]["text"][:200])
        check("and says plainly that the conversation is not being continued",
              "cannot read" in switches[0]["text"], switches[0]["text"][:200])
        check("hung off the last turn, so it sorts after that turn's output",
              switches[0]["run_id"] == max(r_["id"] for r_ in transcript["runs"]),
              str(switches[0]["run_id"]))

    # --- the briefing ------------------------------------------------------
    sent.clear()
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": SECOND_TASK})
    runs = settle(c, sid)
    handoff_run = next((r_ for r_ in runs if r_["carries_handoff"]), None)
    check("exactly one turn is marked as carrying the briefing",
          sum(1 for r_ in runs if r_["carries_handoff"]) == 1,
          str([(r_["id"], r_["carries_handoff"]) for r_ in runs]))
    check("the handoff turn ran on codex", handoff_run and handoff_run["provider"] == "codex",
          str(handoff_run and handoff_run["provider"]))
    check("and succeeded", handoff_run and handoff_run["status"] == "succeeded",
          str(handoff_run and (handoff_run["status"], handoff_run["error"]))[:300])

    check("codex was the CLI that got this turn", [p for p, _ in sent] == ["codex"],
          str([p for p, _ in sent]))
    briefed = sent[0][1] if sent else ""
    check("the prompt handed to the CLI opens with the briefing",
          briefed.startswith("--- HANDOFF BRIEFING"), briefed[:120])
    check("which tells the incoming agent it is taking over somebody else's work",
          "taking over" in briefed and "not your own history" in briefed,
          briefed[:400])
    check("names the agent that was here before",
          "Claude" in briefed, briefed[:400])
    check("carries what the operator actually asked earlier",
          FIRST_TASK in briefed, briefed[:600])
    check("and what the previous agent replied",
          "describes a sample project" in briefed, briefed[-1200:])
    check("lists the tools it used, so the new agent knows work was done",
          "Read" in briefed, briefed[-1500:])
    check("does not replay tool output, which is where secrets end up",
          "# Demo" not in briefed, briefed[-1500:])
    check("and ends by handing over to the operator's real message",
          briefed.rstrip().endswith(SECOND_TASK), briefed[-300:])

    # The whole point of the split: the transcript is the operator's words.
    stored = next(r_ for r_ in runs if r_["id"] == handoff_run["id"])
    check("run.prompt is exactly what the operator typed",
          stored["prompt"] == SECOND_TASK, stored["prompt"][:200])
    check("with no trace of the briefing in it",
          "HANDOFF BRIEFING" not in stored["prompt"], stored["prompt"][:200])

    check("the debt is settled once the briefing has gone out",
          c.get(f"/api/sessions/{sid}").json()["handoff_pending"] is False)

    # --- exactly once ------------------------------------------------------
    sent.clear()
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "And now the invoices table."})
    runs = settle(c, sid)
    check("the turn after the handoff is a plain turn",
          [p for p, _ in sent] == ["codex"] and "HANDOFF BRIEFING" not in sent[0][1],
          sent[0][1][:200] if sent else "nothing sent")
    check("and is not marked as carrying a briefing",
          sum(1 for r_ in runs if r_["carries_handoff"]) == 1,
          str([(r_["id"], r_["carries_handoff"]) for r_ in runs]))

    # --- history keeps its own labels --------------------------------------
    labels = [(r_["id"], r_["provider"]) for r_ in runs]
    check("the turns from before the switch still say claude",
          [p for _, p in labels[:2]] == ["claude", "claude"], str(labels))
    check("and the ones after it say codex",
          [p for _, p in labels[2:]] == ["codex", "codex"], str(labels))
    check("so the session's current provider is not what labels a turn",
          c.get(f"/api/sessions/{sid}").json()["provider"] == "codex"
          and labels[0][1] == "claude", str(labels))

    # --- switching back ----------------------------------------------------
    # A second handoff, not a return: Codex's thread id is dropped too, and
    # Claude does not get the session it had the first time either.
    codex_thread = c.get(f"/api/sessions/{sid}").json()["provider_session_id"]
    check("codex recorded a thread of its own", bool(codex_thread), str(codex_thread))
    r = c.patch(f"/api/sessions/{sid}", json={"provider": "claude"})
    back = r.json() if r.status_code == 200 else {}
    check("switching back is allowed", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("and is another handoff, not a resume of the original claude session",
          back.get("provider_session_id") is None and back.get("handoff_pending") is True,
          f"{back.get('provider_session_id')} {back.get('handoff_pending')}")
    switches = [
        e for e in c.get(f"/api/sessions/{sid}/transcript").json()["events"]
        if e["kind"] == "provider_switch"
    ]
    check("both switches are in the transcript", len(switches) == 2, str(len(switches)))

    sent.clear()
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "Summarise where we are."})
    settle(c, sid)
    second = sent[0][1] if sent else ""
    check("the returning agent is briefed too", "HANDOFF BRIEFING" in second, second[:120])
    check("and the briefing spans the whole conversation, both agents' turns",
          FIRST_TASK in second and SECOND_TASK in second, second[:200])
    check("naming Codex as the agent it is taking over from",
          "from Codex" in second or "by Codex" in second, second[:800])

    # --- effort that cannot survive the move -------------------------------
    r = c.post("/api/sessions", json={
        "provider": "codex", "model": "gpt-5.6-terra", "effort": "ultra",
        "title": "Ultra", "approval_mode": "auto",
    })
    ultra = r.json()
    check("a codex session can sit at a level claude does not have",
          r.status_code == 201 and ultra["effort"] == "ultra", r.text[:200])
    r = c.patch(f"/api/sessions/{ultra['id']}", json={"provider": "claude"})
    check("switching to claude is not refused over it", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("the impossible level is cleared rather than carried",
          r.json()["effort"] is None, str(r.json()["effort"]))
    check("and the codex model with it", r.json()["model"] is None, str(r.json()["model"]))

    # A caller may name the replacement itself, and then it is theirs to get right.
    r = c.post("/api/sessions", json={
        "provider": "codex", "model": "gpt-5.5", "effort": "high", "approval_mode": "auto",
    })
    picky = r.json()["id"]
    r = c.patch(f"/api/sessions/{picky}", json={
        "provider": "claude", "model": "haiku", "effort": "max",
    })
    check("a switch can name the new model and level in the same request",
          r.status_code == 200 and r.json()["model"] == "haiku"
          and r.json()["effort"] == "max", f"{r.status_code} {r.text[:200]}")

    # --- choosing a provider before the first turn is not a handoff --------
    fresh = c.post("/api/sessions", json={"provider": "claude", "title": "Not started"}).json()
    r = c.patch(f"/api/sessions/{fresh['id']}", json={"provider": "codex"})
    check("an unstarted session can change provider", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("and owes nobody a briefing, because there is no conversation",
          r.json()["handoff_pending"] is False, str(r.json()["handoff_pending"]))
    check("nothing is written into an empty transcript",
          c.get(f"/api/sessions/{fresh['id']}/transcript").json()["events"] == [])

    # Re-stating the provider a session already runs on is not a switch, and
    # must not throw its session id away.
    kept = c.get(f"/api/sessions/{sid}").json()
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "keep going"})
    settle(c, sid)
    have = c.get(f"/api/sessions/{sid}").json()["provider_session_id"]
    r = c.patch(f"/api/sessions/{sid}", json={"provider": kept["provider"]})
    check("naming the provider it already runs on changes nothing",
          r.status_code == 200 and r.json()["provider_session_id"] == have
          and r.json()["handoff_pending"] is False,
          f"{r.status_code} {r.text[:200]}")


# ---------------------------------------------------------------------------
# The digest itself: the cap, the omission notice, and the redaction pass.
# Driven directly rather than over HTTP, because what has to be pinned is the
# arithmetic — a briefing one character over its budget is a bug nobody would
# see through the UI, and a secret surviving it is one nobody would see at all.
# ---------------------------------------------------------------------------
LEAK = "AIOPS_SECRET_KEY=" + "S3cretValueNobodyShouldSee"
BARE = "hunter2hunter2hunter2hunter2"


async def digest_checks():
    async with SessionLocal() as db:
        # Six long turns, so a small budget must drop some of them.
        big = Session(provider="codex", model="gpt-5.5", title="Cap", owner_id=1)
        db.add(big)
        await db.flush()
        for i in range(1, 7):
            run = Run(
                session_id=big.id, prompt=f"TURN{i}-ASK " + "q" * 400,
                provider="claude", model="opus", status="succeeded",
            )
            db.add(run)
            await db.flush()
            db.add(Event(
                run_id=run.id, session_id=big.id, seq=1, kind="assistant",
                text=f"TURN{i}-REPLY " + "r" * 900,
            ))
        await db.commit()

        for budget in (4000, 6000, 9000):
            out = await handoff.build_digest(db, big, before_run_id=10**9, budget=budget)
            check(f"a {budget}-character budget is not exceeded",
                  len(out) <= budget, f"{len(out)} chars")
            check(f"and the newest turn survives at {budget}",
                  "TURN6-ASK" in out, out[-400:])
            check(f"the {budget}-character briefing admits it is incomplete",
                  "THIS BRIEFING IS INCOMPLETE" in out, out[:400])
            check(f"and the oldest turn is what went, at {budget}",
                  "TURN1-ASK" not in out, out[:400])

        # A cap smaller than the briefing's own framing has no honest answer:
        # obeying it produces a preamble telling the agent it has inherited a
        # conversation, followed by nothing about the conversation. So the floor
        # wins and the briefing still carries the newest turn.
        floored = await handoff.build_digest(db, big, before_run_id=10**9, budget=10)
        check("a cap too small for the framing is raised rather than obeyed",
              "TURN6-ASK" in floored and len(floored) > 10, f"{len(floored)} chars")
        check("but only to the floor, not to something unbounded",
              len(floored) < 4000, f"{len(floored)} chars")

        roomy = await handoff.build_digest(db, big, before_run_id=10**9, budget=200_000)
        check("with room for everything, nothing is dropped",
              all(f"TURN{i}-ASK" in roomy for i in range(1, 7)), str(len(roomy)))
        check("and it does not claim to be incomplete",
              "THIS BRIEFING IS INCOMPLETE" not in roomy, roomy[:300])

        # Only the turns before the one being briefed: the operator's new message
        # is what the CLI is being sent, and repeating it above itself would read
        # as the operator having said the same thing twice.
        first_run_id = await db.scalar(
            select(func.min(Run.id)).where(Run.session_id == big.id)
        )
        check("a briefing for the very first turn has nothing to say",
              await handoff.build_digest(db, big, before_run_id=first_run_id) == "")

        # --- secrets ---
        leaky = Session(provider="codex", title="Leaky", owner_id=1)
        db.add(leaky)
        await db.flush()
        run = Run(
            session_id=leaky.id, provider="claude", model="opus", status="succeeded",
            prompt="Check the container's configuration.",
        )
        db.add(run)
        await db.flush()
        db.add_all([
            Event(run_id=run.id, session_id=leaky.id, seq=1, kind="tool_use",
                  tool_name="Bash", text="env | sort"),
            # The incident: a whole environment in a tool result.
            Event(run_id=run.id, session_id=leaky.id, seq=2, kind="tool_result",
                  text=f"PATH=/usr/bin\n{LEAK}\nHOSTNAME=aiops\nTOOLRESULTCANARY=1"),
            # And the subtler one: the agent repeating it in its own words.
            Event(run_id=run.id, session_id=leaky.id, seq=3, kind="assistant",
                  text=f"The key is {SECRET} — also {LEAK}, and the token is {BARE}."),
        ])
        await db.commit()

        # Switched away and straight back before the other provider ever
        # answered. The briefing is still owed — the round trip threw the
        # resumable session id away — but telling Codex it is taking over from
        # Codex would read as nonsense and invite it to distrust the summary.
        loop = Session(provider="codex", title="Round trip", owner_id=1)
        db.add(loop)
        await db.flush()
        run = Run(session_id=loop.id, provider="codex", model="gpt-5.5",
                  status="succeeded", prompt="the tag is ROUNDTRIPCANARY")
        db.add(run)
        await db.flush()
        db.add(Event(run_id=run.id, session_id=loop.id, seq=1, kind="assistant",
                     text="Noted the tag."))
        await db.commit()
        out = await handoff.build_digest(db, loop, before_run_id=10**9)
        check("a round trip briefs the same agent as itself-in-another-session",
              "an earlier session of Codex that you cannot reopen" in out, out[:400])
        check("rather than as a different agent it has never met",
              "by Codex, in a separate session" not in out, out[:400])
        check("and still carries the conversation", "ROUNDTRIPCANARY" in out, out[-500:])

        out = await handoff.build_digest(db, leaky, before_run_id=10**9)
        check("a secret named in a tool result never reaches the briefing",
              "S3cretValueNobodyShouldSee" not in out, out[-800:])
        check("because tool output is not replayed at all",
              "TOOLRESULTCANARY" not in out, out[-800:])
        check("a secret the agent repeated in its own reply is masked",
              SECRET not in out and redaction.MASK in out, out[-800:])
        check("including one it merely narrated, with no punctuation to key on",
              BARE not in out, out[-800:])
        check("and a command that dumps the environment is noted, not quoted",
              "env | sort" not in out and "reads the environment" in out, out[-800:])


async def backfill_checks():
    """The upgrade, run for real rather than inspected.

    An unlabelled turn is the case that matters: every row in a database that
    predates this feature has no provider, and the UI would otherwise draw all of
    them as whatever the session is set to now — which is exactly the retroactive
    relabelling the column exists to prevent.
    """
    async with SessionLocal() as db:
        sess = Session(provider="codex", model="gpt-5.6-sol", title="Old", owner_id=1)
        db.add(sess)
        await db.flush()
        old = Run(session_id=sess.id, prompt="from before the column existed",
                  status="succeeded")
        db.add(old)
        await db.commit()
        # Straight to SQL: the ORM would helpfully fill both in.
        await db.execute(
            text("UPDATE runs SET provider = NULL, model = NULL WHERE id = :id"),
            {"id": old.id},
        )
        await db.commit()

    await migrate._backfill_run_providers()

    async with SessionLocal() as db:
        row = await db.get(Run, old.id)
        check("an unlabelled turn is given its session's provider on upgrade",
              row.provider == "codex", str(row.provider))
        check("and its model", row.model == "gpt-5.6-sol", str(row.model))


asyncio.run(digest_checks())
asyncio.run(backfill_checks())


# --- the argv the briefing has to survive ----------------------------------
# Found by running this against the real CLI, not by reading the code: every
# briefing begins with a `---` rule, and `codex exec` read that as an option and
# died in argument parsing before the model was ever called. Any operator message
# starting with a dash was already failing the same way. The original build_run is
# used here because this suite has replaced the patched one with a stand-in.
raw = _codex_build(
    PROVIDERS["codex"],
    prompt="--- HANDOFF BRIEFING: you are taking over ---\nthe rest of it",
    model=None,
    provider_session_id=None,
    permission_mode=None,
    system_prompt=None,
    allowed_tools=None,
    extra_args=[],
    stream_partials=False,
)
check("codex is given its prompt after a `--` separator",
      raw.argv[-2] == "--", " ".join(raw.argv[-3:-1]))
check("so a prompt opening with a rule is still a prompt",
      raw.argv[-1].startswith("--- HANDOFF BRIEFING"), raw.argv[-1][:60])
check("and the flags before it are untouched",
      "--json" in raw.argv and "--skip-git-repo-check" in raw.argv, " ".join(raw.argv[:6]))


# --- the redaction pass on its own -----------------------------------------
for label, sample, gone in (
    ("a shell assignment", "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrst", "ghp_abcdef"),
    ("a yaml key", "  db_password: correct-horse-battery", "correct-horse"),
    ("an inline api key", "used API_KEY=abcd1234efgh5678 for that", "abcd1234efgh5678"),
    ("a bearer token", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("a private key block",
     "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END OPENSSH PRIVATE KEY-----",
     "b3BlbnNzaA"),
    ("an openai-shaped key", "the key sk-abcdefghijklmnopqrstuvwx leaked", "sk-abcdefghij"),
    ("one narrated in prose", "the passphrase is Tr0ubador&3-horse-staple",
     "Tr0ubador&3-horse-staple"),
):
    out = redaction.redact(sample)
    check(f"redaction masks {label}", gone not in out, out[:120])

for label, sample in (
    ("a plain sentence", "The migration splits billing into two tables."),
    # Over-masking is not free: a briefing full of [redacted] where the agent
    # was discussing its work is a briefing nobody can act on.
    ("prose that merely mentions one", "The password was changed and the token rotated."),
):
    check(f"redaction leaves {label} alone", redaction.redact(sample) == sample,
          redaction.redact(sample))
check("it does not mask the shipped jwt placeholder, which is not a secret",
      "change-me-please" in redaction.redact("the default is change-me-please"))

for command, expected in (
    ("env", True),
    ("env | sort", True),
    ("printenv AIOPS_SECRET_KEY", True),
    ("cat /proc/1/environ", True),
    ("cat .env", True),
    ("sudo env", True),
    ("pytest -q", False),
    ("git commit -m 'set the environment up'", False),
):
    got = redaction.dumps_environment(command)
    check(f"{command!r} is {'' if expected else 'not '}an environment dump",
          got is expected, str(got))


# --- the upgrade path ------------------------------------------------------
# `create_all` never alters an existing table, so a column added to the model
# without an entry here exists only on fresh installs — and on a deployed
# database every run fetch would 500 on the missing column.
for table, column, ddl in (
    ("runs", "provider", "VARCHAR(32)"),
    ("runs", "model", "VARCHAR(128)"),
    ("runs", "carries_handoff", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("sessions", "handoff_pending", "BOOLEAN NOT NULL DEFAULT FALSE"),
):
    check(f"an existing {table} table gains {column} on upgrade",
          migrate.COLUMNS.get(table, {}).get(column) == ddl,
          str(migrate.COLUMNS.get(table, {}).get(column)))
check("the backfill runs as part of the migration, not only when called by hand",
      "_backfill_run_providers" in migrate.run_migrations.__code__.co_names,
      str(migrate.run_migrations.__code__.co_names))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
