"""Naming a session up front, and reasoning effort from the UI to the argv.

Two features that are easy to ship half-wired, in opposite ways. A title
accepted by the API but never sent by the form looks like the field does
nothing; an effort stored on the session but never put on the command line
looks like it works and quietly runs at the model's default. So the checks here
walk the whole path: the HTTP surface, the preset, the fallback between them,
and the exact flag each CLI actually accepts.

The model list is pinned against what `codex debug models` reported for
codex-cli 0.147.0. It shipped naming three models that catalog has never heard
of, which the UI offered and every run using one would have failed on.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-effort.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-effort-test-ws-")
)

for _stale in ("./test-effort.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

from fastapi.testclient import TestClient  # noqa: E402

from app import migrate  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentPreset, Session  # noqa: E402
from app.providers import PROVIDERS  # noqa: E402
from app.providers.codex_appserver import CodexAppServerAdapter  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def build(provider, **kwargs):
    """One turn's argv, with everything the runner passes defaulted away."""
    spec = PROVIDERS[provider].build_run(
        prompt="do the thing",
        model=kwargs.pop("model", None),
        provider_session_id=None,
        permission_mode=None,
        system_prompt=None,
        allowed_tools=None,
        extra_args=[],
        stream_partials=False,
        **kwargs,
    )
    return spec.argv


# --- what the CLIs actually accept ----------------------------------------
claude = PROVIDERS["claude"]
codex = PROVIDERS["codex"]

check("claude advertises the levels its --effort flag lists",
      claude.efforts == ["low", "medium", "high", "xhigh", "max"],
      str(claude.efforts))
check("codex advertises the levels its model catalog lists",
      codex.efforts == ["low", "medium", "high", "xhigh", "max", "ultra"],
      str(codex.efforts))

check("the three models Codex has never heard of are gone",
      not {"gpt-5.6", "gpt-5.6-codex", "gpt-5-codex"} & set(codex.models),
      str(codex.models))
check("the sol/terra/luna family is offered",
      {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= set(codex.models),
      str(codex.models))

# Per-model narrowing: luna stops at max, the 5.4/5.5 family at xhigh.
check("terra offers every level", codex.effort_choices("gpt-5.6-terra") == codex.efforts)
check("luna stops at max, as the catalog says",
      codex.effort_choices("gpt-5.6-luna") == ["low", "medium", "high", "xhigh", "max"],
      str(codex.effort_choices("gpt-5.6-luna")))
check("gpt-5.5 stops at xhigh",
      codex.effort_choices("gpt-5.5") == ["low", "medium", "high", "xhigh"],
      str(codex.effort_choices("gpt-5.5")))
check("an unknown model gets the full list rather than none",
      codex.effort_choices("something-new") == codex.efforts)
check("no model narrows claude's list", claude.effort_choices("opus") == claude.efforts)


# --- the flag on the command line ----------------------------------------
argv = build("claude", effort="xhigh")
check("claude puts the level behind --effort",
      "--effort" in argv and argv[argv.index("--effort") + 1] == "xhigh",
      " ".join(argv))
check("and leaves it off entirely when nothing was chosen",
      "--effort" not in build("claude"), " ".join(build("claude")))

argv = build("codex", effort="ultra")
# There is no --effort on this binary; it is a config override, and the key name
# was confirmed against `--strict-config`, which rejects a misspelling.
check("codex sets the level as a config override",
      "-c" in argv and "model_reasoning_effort=ultra" in argv, " ".join(argv))
check("codex does not invent an --effort flag it would reject",
      "--effort" not in argv, " ".join(argv))
check("and nothing is overridden when nothing was chosen",
      not any(a.startswith("model_reasoning_effort") for a in build("codex")),
      " ".join(build("codex")))

# The interactive Codex path is a different adapter, and it carries the level in
# the JSON-RPC turn rather than on argv.
adapter = CodexAppServerAdapter(prompt="p", cwd=".", model="gpt-5.6-sol", effort="max")
adapter.conversation_id = "thread-1"
check("the app-server turn carries the effort",
      adapter.turn_params().get("effort") == "max", str(adapter.turn_params()))
plain = CodexAppServerAdapter(prompt="p", cwd=".")
plain.conversation_id = "thread-1"
check("and omits the key rather than sending null",
      "effort" not in plain.turn_params(), str(plain.turn_params()))


# --- the fallback the runner reads ---------------------------------------
preset = AgentPreset(id=1, name="Ops", provider="codex", effort="high", extra_args=[])
check("a session with no effort of its own inherits its preset's",
      Session(provider="codex", preset=preset).effective_effort == "high")
check("and its own choice wins over the preset",
      Session(provider="codex", effort="low", preset=preset).effective_effort == "low")
check("with neither set, nothing is forced on the CLI",
      Session(provider="codex").effective_effort is None)


# --- the upgrade path -----------------------------------------------------
# `create_all` never alters an existing table, so a column added to the model
# without an entry here exists only on fresh installs. On a deployed database
# every session fetch would 500 on the missing column, which is why this is
# checked rather than trusted — the suites above all start from an empty file.
for table in ("sessions", "agent_presets"):
    check(f"an existing {table} table gets the effort column on upgrade",
          migrate.COLUMNS.get(table, {}).get("effort") == "VARCHAR(16)",
          str(migrate.COLUMNS.get(table, {}).get("effort")))


# --- over HTTP ------------------------------------------------------------
with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    r = c.get("/api/providers")
    body = r.json()
    by_name = {p["name"]: p for p in body}
    check("the API tells the UI which levels exist", r.status_code == 200
          and by_name["codex"]["efforts"] == codex.efforts, r.text[:200])
    check("including the per-model narrowing, so the picker cannot offer a bad one",
          by_name["codex"]["efforts_by_model"].get("gpt-5.6-luna")
          == ["low", "medium", "high", "xhigh", "max"],
          str(by_name["codex"]["efforts_by_model"])[:200])

    # --- naming a session when creating it ---
    r = c.post("/api/sessions", json={
        "provider": "claude", "title": "Weekly dependency sweep",
    })
    check("a session can be named on creation", r.status_code == 201, r.text[:200])
    named = r.json() if r.status_code == 201 else {}
    check("and keeps the name it was given",
          named.get("title") == "Weekly dependency sweep", str(named.get("title")))

    # The name must survive the first turn, which is what used to overwrite it.
    r = c.post(f"/api/sessions/{named['id']}/prompt", json={"prompt": "check for CVEs"})
    check("sending the first task does not rename it",
          c.get(f"/api/sessions/{named['id']}").json()["title"] == "Weekly dependency sweep",
          c.get(f"/api/sessions/{named['id']}").json()["title"])

    # Blank is still auto-named, so the field is optional rather than required.
    r = c.post("/api/sessions", json={
        "provider": "claude", "title": "   ", "prompt": "Summarise yesterday's alerts",
    })
    auto = r.json()
    check("a blank name still falls back to the first task",
          auto.get("title") == "Summarise yesterday's alerts", str(auto.get("title")))
    r = c.post("/api/sessions", json={"provider": "claude"})
    check("and a session with neither is Untitled until a task arrives",
          r.json().get("title") == "Untitled", str(r.json().get("title")))

    # --- effort through the session ---
    r = c.post("/api/sessions", json={
        "provider": "codex", "model": "gpt-5.6-terra", "effort": "ultra",
        "title": "High effort run",
    })
    check("a session can be created at a chosen effort", r.status_code == 201, r.text[:300])
    sess = r.json() if r.status_code == 201 else {}
    check("and reports it back", sess.get("effort") == "ultra", str(sess.get("effort")))
    check("it is still there on a plain fetch",
          c.get(f"/api/sessions/{sess['id']}").json()["effort"] == "ultra")

    r = c.patch(f"/api/sessions/{sess['id']}", json={"effort": "low"})
    check("the effort can be changed afterwards",
          r.status_code == 200 and r.json()["effort"] == "low", r.text[:200])
    r = c.patch(f"/api/sessions/{sess['id']}", json={"effort": None})
    check("and cleared back to the model's default",
          r.status_code == 200 and r.json()["effort"] is None, r.text[:200])

    # --- what must be refused ---
    r = c.post("/api/sessions", json={"provider": "codex", "effort": "banana"})
    check("a level neither CLI has is refused", r.status_code == 400, str(r.status_code))
    check("and the refusal lists what is allowed",
          "low" in r.text and "ultra" in r.text, r.text[:200])

    # The one that matters: Codex takes any string for this config key and only
    # fails once the turn is already running, so a level the *model* does not
    # accept has to be caught here or it looks like it worked.
    r = c.post("/api/sessions", json={
        "provider": "codex", "model": "gpt-5.5", "effort": "ultra",
    })
    check("a level the chosen model does not accept is refused, not silently downgraded",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    check("and the refusal names the model",
          "gpt-5.5" in r.text, r.text[:200])

    r = c.post("/api/sessions", json={"provider": "claude", "effort": "ultra"})
    check("claude refuses a level only Codex has", r.status_code == 400, str(r.status_code))

    # Re-pointing an existing session at a narrower model must be caught too.
    r = c.post("/api/sessions", json={
        "provider": "codex", "model": "gpt-5.6-terra", "effort": "ultra",
    })
    wide = r.json()
    r = c.patch(f"/api/sessions/{wide['id']}", json={"model": "gpt-5.5"})
    check("switching to a narrower model is refused while the old level stands",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/sessions/{wide['id']}", json={"model": "gpt-5.5", "effort": "high"})
    check("changing both at once is allowed",
          r.status_code == 200 and r.json()["effort"] == "high", r.text[:200])

    # --- effort through the preset ---
    r = c.post("/api/presets", json={
        "name": "Deep Codex", "provider": "codex", "model": "gpt-5.6-sol",
        "effort": "max",
    })
    check("a preset can pin an effort", r.status_code == 201, r.text[:300])
    check("and reports it back", r.status_code == 201 and r.json()["effort"] == "max",
          r.text[:200])
    preset_id = r.json()["id"] if r.status_code == 201 else None
    check("it survives the round trip through the list",
          any(p["id"] == preset_id and p["effort"] == "max" for p in c.get("/api/presets").json()))

    r = c.post("/api/presets", json={
        "name": "Impossible", "provider": "codex", "model": "gpt-5.4-mini",
        "effort": "ultra",
    })
    check("a preset cannot pin a level its model refuses", r.status_code == 400,
          f"{r.status_code} {r.text[:200]}")
    r = c.post("/api/presets", json={
        "name": "Also impossible", "provider": "claude", "effort": "nonsense",
    })
    check("nor an invented one", r.status_code == 400, str(r.status_code))

    # A session on that preset and nothing of its own runs at the preset's level:
    # the property the runner reads to build the command.
    r = c.post("/api/sessions", json={"provider": "codex", "preset_id": preset_id})
    check("a session on that preset stores no effort of its own",
          r.status_code == 201 and r.json()["effort"] is None, r.text[:200])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
