"""Covers what a shared session does with a member's own stored credentials.

The rule being tested is deliberately permissive: a turn reaches the systems the
person who *asked for it* can reach, so bringing your own systems into somebody
else's conversation works. That is the behaviour Jordan asked for and the last
group of checks here exists to prove it has not been quietly narrowed — an
"intersection" rule (only systems every viewer can reach) would pass every other
check in this file while removing the capability.

What is added is the disclosure around it, and that is what the rest covers: the
facts are computed server-side from the same visibility rules as everything else,
the person whose key it is is asked once before the first such turn, they are
asked again if the audience grows, they are never asked when there is nothing at
stake, and the transcript records each turn that actually used a stored system in
front of other people.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-exposure.db")
DB_FILE = os.environ["AIOPS_DATABASE_URL"].split("///", 1)[-1]
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
# Without this the API refuses to store a credential at all, and every check
# below that needs a system to exist would pass for the wrong reason.
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault(
    "AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-exposure-attach-")
)
# Real turns are driven below — the runner has to reach the point of
# materialising credentials — so the workspace root must be writable.
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-exposure-ws-")
)
os.environ.setdefault(
    "AIOPS_ACCOUNTS_ROOT", tempfile.mkdtemp(prefix="aiops-exposure-accounts-")
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def login(client, username, password="devpassword123"):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def make_user(client, username):
    r = client.post("/api/users", json={
        "username": username, "password": f"{username}password1",
        "is_admin": False, "must_change_password": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def as_user(client, username):
    login(client, username, f"{username}password1")


# A throwaway key, so no real credential is ever in the repo and the systems
# below have something a run can actually materialise.
tmpdir = tempfile.mkdtemp(prefix="aiops-exposure-key-")
key_path = os.path.join(tmpdir, "probe")
subprocess.run(
    ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-q"],
    check=True, capture_output=True,
)
with open(key_path) as fh:
    PRIVATE_KEY = fh.read()


def settle(client, run_id, seconds=30):
    """Wait for a turn to stop being queued or running.

    Neither agent CLI is on PATH in the suite runner, so a real turn fails almost
    at once — after the point where credentials are materialised and the
    transcript note is written, which is what these checks are about.
    """
    if run_id is None:
        return {}
    deadline = time.time() + seconds
    row = {}
    while time.time() < deadline:
        row = client.get(f"/api/runs/{run_id}").json()
        if row.get("status") not in ("queued", "running"):
            return row
        time.sleep(0.25)
    return row


def usernames(rows):
    return sorted(r["username"] for r in rows)


def exposure_events(client, session_id):
    """Transcript notes recording that stored systems were used in front of others."""
    body = client.get(f"/api/sessions/{session_id}/transcript").json()
    out = []
    for event in body["events"]:
        if event["kind"] != "system":
            continue
        raw = client.get(
            f"/api/sessions/{session_id}/events/{event['id']}/raw"
        ).json()["raw"]
        if isinstance(raw, dict) and "aiops_credential_exposure" in raw:
            out.append((event, raw["aiops_credential_exposure"]))
    return out


with TestClient(app) as c:
    login(c, "admin")
    alice = make_user(c, "alice")
    bob = make_user(c, "bob")
    carol = make_user(c, "carol")
    dave = make_user(c, "dave")

    # Bob is the one with a credential of his own. Alice cannot reach it, which
    # is the whole point: she is about to be able to read what it produces.
    as_user(c, "bob")
    r = c.post("/api/targets", json={
        "name": "Bob Prod", "hostname": "10.9.9.9", "username": "bob",
        "auth_type": "key", "private_key": PRIVATE_KEY,
    })
    check("the member with a credential can store it", r.status_code == 201, r.text[:200])
    bob_system = r.json()["id"] if r.status_code == 201 else None

    # Carol has one too, so "only the caller's own systems" is a real check
    # rather than a check against an empty list.
    as_user(c, "carol")
    c.post("/api/targets", json={
        "name": "Carol Box", "hostname": "10.9.9.10", "username": "carol",
        "auth_type": "key", "private_key": PRIVATE_KEY,
    })

    # --- a session Alice owns and Bob was let into --------------------------
    as_user(c, "alice")
    shared = c.post("/api/sessions", json={
        "provider": "claude", "title": "alice's shared session",
        "approval_mode": "bypass",
    }).json()["id"]
    r = c.patch(f"/api/sessions/{shared}", json={"shared_user_ids": [bob]})
    check("the owner can share it", r.status_code == 200, r.text[:200])

    # --- the exposure endpoint: sharee's view -------------------------------
    as_user(c, "bob")
    r = c.get(f"/api/sessions/{shared}/exposure")
    check("a sharee can ask what a turn of theirs would expose",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    view = r.json() if r.status_code == 200 else {}
    check("it names the other people who can read the session",
          usernames(view.get("viewers", [])) == ["alice"], str(view.get("viewers"))[:200])
    check("and does not count the caller as an audience for themselves",
          all(v["username"] != "bob" for v in view.get("viewers", [])))
    check("it names the caller's own systems, by the short name an agent types",
          [s["slug"] for s in view.get("systems", [])] == ["bob-prod"],
          str(view.get("systems"))[:200])
    check("and nobody else's — carol's system is not the caller's business",
          all(s["slug"] != "carol-box" for s in view.get("systems", [])),
          str(view.get("systems"))[:200])
    check("no credential state or hostname rides along with it",
          all(set(s) == {"id", "name", "slug"} for s in view.get("systems", [])),
          str(view.get("systems"))[:200])
    check("with both halves present, there is something to warn about",
          view.get("at_stake") is True and view.get("needs_acknowledgement") is True,
          str(view)[:200])
    check("and nothing is recorded as agreed to yet",
          view.get("acknowledged") is False and view.get("acknowledged_at") is None)
    check("everyone who can read it is a viewer the confirmation is about",
          usernames(view.get("new_viewers", [])) == ["alice"])

    # --- the owner's view of the same session -------------------------------
    as_user(c, "alice")
    r = c.get(f"/api/sessions/{shared}/exposure")
    check("the owner sees the same audience from their side",
          r.status_code == 200 and usernames(r.json()["viewers"]) == ["bob"],
          r.text[:200])
    check("but with no systems of their own, nothing is at stake for them",
          r.json()["systems"] == [] and r.json()["at_stake"] is False, r.text[:200])
    check("so the owner is never asked to confirm anything",
          r.json()["needs_acknowledgement"] is False)

    # --- somebody who was not let in ----------------------------------------
    as_user(c, "dave")
    r = c.get(f"/api/sessions/{shared}/exposure")
    check("a non-viewer gets 404, not a list of who is in the room",
          r.status_code == 404, f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/sessions/{shared}/exposure/ack", json={})
    check("and cannot record an acknowledgement on a session they cannot see",
          r.status_code == 404, str(r.status_code))

    # --- the acknowledgement is required, once ------------------------------
    as_user(c, "bob")
    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "look at bob-prod"})
    check("the first turn is refused until the exposure is acknowledged",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    check("and the refusal names who would be able to read the result",
          "alice" in r.text, r.text[:300])
    check("it is not framed as a permission problem",
          "restriction" in r.text or "disclosure" in r.text, r.text[:300])

    r = c.post(f"/api/sessions/{shared}/exposure/ack",
               json={"viewer_ids": [alice + 999]})
    check("agreeing to an audience that is not the real one is refused",
          r.status_code == 409, f"{r.status_code} {r.text[:200]}")

    r = c.post(f"/api/sessions/{shared}/exposure/ack", json={"viewer_ids": [alice]})
    check("agreeing to the audience on screen is recorded",
          r.status_code == 200 and r.json()["acknowledged"] is True,
          f"{r.status_code} {r.text[:200]}")
    check("and it carries when, so it can be cited afterwards",
          r.status_code == 200 and r.json()["acknowledged_at"] is not None)
    check("the warning stays true — this was a disclosure, not a dismissal",
          r.status_code == 200 and r.json()["at_stake"] is True
          and r.json()["needs_acknowledgement"] is False, r.text[:200])

    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "now look at bob-prod"})
    check("the turn goes through once it has been acknowledged",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    first_run = r.json()["id"] if r.status_code == 202 else None
    settled = settle(c, first_run)

    # --- capability unchanged: the requester's systems, in a shared session --
    # This is the check that would fail under an intersection rule. Alice cannot
    # reach bob-prod and it is her session, and the turn still gets it.
    notes = exposure_events(c, shared)
    check("a turn in a shared session still gets the requester's own systems",
          len(notes) == 1 and [s["slug"] for s in notes[0][1]["systems"]] == ["bob-prod"],
          str(notes)[:300])
    check("even though the session's owner cannot reach that system herself",
          len(notes) == 1 and notes[0][1]["used_by"] == "bob", str(notes)[:200])

    # --- the transcript records the consent --------------------------------
    check("the transcript says which systems were used",
          len(notes) == 1 and "bob-prod" in (notes[0][0]["text"] or ""),
          str(notes[:1])[:300])
    check("and who could read what they produced",
          len(notes) == 1 and "alice" in (notes[0][0]["text"] or ""),
          str(notes[:1])[:300])
    check("and whose credentials they were",
          len(notes) == 1 and "bob" in (notes[0][0]["text"] or ""))
    check("the record names the readers in a form a later reader can resolve",
          len(notes) == 1 and usernames(notes[0][1]["readable_by"]) == ["alice"],
          str(notes)[:200])
    check("and cites the acknowledgement, so 'who agreed to this' is answerable",
          len(notes) == 1 and notes[0][1]["acknowledged_at"] is not None,
          str(notes)[:200])
    check("it is a system note in the transcript, not a hidden column",
          len(notes) == 1 and notes[0][0]["kind"] == "system")
    check("the turn itself ran and settled",
          settled.get("status") not in (None, "queued", "running"),
          str(settled.get("status")))

    # --- and is not asked again ---------------------------------------------
    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "and again"})
    check("a later turn is not held up by the same question",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    second_run = r.json()["id"] if r.status_code == 202 else None
    settle(c, second_run)
    check("but each turn that used them is recorded separately",
          len(exposure_events(c, shared)) == 2,
          str(len(exposure_events(c, shared))))

    # --- re-arming: consenting to Bob is not consenting to Carol ------------
    as_user(c, "alice")
    r = c.patch(f"/api/sessions/{shared}", json={"shared_user_ids": [bob, carol]})
    check("the owner can add somebody afterwards", r.status_code == 200, r.text[:200])

    as_user(c, "bob")
    r = c.get(f"/api/sessions/{shared}/exposure")
    view = r.json()
    check("a viewer added later re-arms the question",
          view["needs_acknowledgement"] is True, str(view)[:250])
    check("and only the new person is what the fresh confirmation is about",
          usernames(view["new_viewers"]) == ["carol"], str(view["new_viewers"]))
    check("the earlier agreement is still on record",
          view["acknowledged_at"] is not None and view["acknowledged"] is False)
    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "with carol watching"})
    check("so the next turn is held again",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    check("and the second refusal names the person who was added",
          "carol" in r.text, r.text[:300])
    r = c.post(f"/api/sessions/{shared}/exposure/ack",
               json={"viewer_ids": sorted([alice, carol])})
    check("agreeing to the larger audience clears it",
          r.status_code == 200 and r.json()["needs_acknowledgement"] is False,
          f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "with carol watching"})
    check("and the turn goes through", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    settle(c, r.json()["id"] if r.status_code == 202 else None)

    # --- nothing shared, nothing asked -------------------------------------
    as_user(c, "bob")
    private = c.post("/api/sessions", json={
        "provider": "claude", "title": "bob alone", "approval_mode": "bypass",
    }).json()["id"]
    r = c.get(f"/api/sessions/{private}/exposure")
    view = r.json()
    check("a session with no other viewers exposes nothing",
          view["viewers"] == [] and view["at_stake"] is False, r.text[:200])
    check("so no acknowledgement is required at all",
          view["needs_acknowledgement"] is False)
    r = c.post(f"/api/sessions/{private}/prompt", json={"prompt": "just me"})
    check("and a turn goes straight through with no confirmation",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    settle(c, r.json()["id"] if r.status_code == 202 else None)
    check("with nothing written into the transcript about exposure",
          exposure_events(c, private) == [], str(exposure_events(c, private))[:200])

    # --- a member with no systems of their own -----------------------------
    as_user(c, "alice")
    r = c.post(f"/api/sessions/{shared}/prompt", json={"prompt": "alice's own turn"})
    check("somebody with no stored systems is never asked either",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    alice_run = r.json()["id"] if r.status_code == 202 else None
    settle(c, alice_run)
    check("and their turn is not recorded as exposing anything",
          all(e[0]["run_id"] != alice_run for e in exposure_events(c, shared)),
          str(exposure_events(c, shared))[:200])

    # --- the team route into a session -------------------------------------
    login(c, "admin")
    team = c.post("/api/teams", json={
        "name": "Night Shift", "member_ids": [alice, bob]
    }).json()["id"]
    as_user(c, "alice")
    teamed = c.post("/api/sessions", json={
        "provider": "claude", "title": "the team's", "team_id": team,
        "approval_mode": "bypass",
    }).json()["id"]
    as_user(c, "bob")
    r = c.get(f"/api/sessions/{teamed}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("team membership counts as being able to read it",
          r.status_code == 200 and usernames(view.get("viewers", [])) == ["alice"],
          f"{r.status_code} {r.text[:200]}")
    check("so a team session asks the same question",
          view.get("needs_acknowledgement") is True, str(view)[:200])
    r = c.post(f"/api/sessions/{teamed}/prompt", json={"prompt": "in the team room"})
    check("and holds the first turn in it",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")

    # A session created straight into a team is shared before its first word,
    # so creating one with an opening prompt must not be a way around the check.
    r = c.post("/api/sessions", json={
        "provider": "claude", "title": "bob's team session", "team_id": team,
        "approval_mode": "bypass", "prompt": "opening move",
    })
    check("creating a shared session with an opening prompt is held too",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    listed = c.get("/api/sessions").json()
    check("and the refused creation leaves no half-made session behind",
          all(s["title"] != "bob's team session" for s in listed),
          str([s["title"] for s in listed])[:200])

    # An admin is not a viewer: administering AIOps is not a way into a session,
    # so an admin must not appear in the audience and inflate the warning.
    as_user(c, "bob")
    check("an admin who was not let in is not counted as a reader",
          all(v["username"] != "admin"
              for v in c.get(f"/api/sessions/{shared}/exposure").json()["viewers"]),
          c.get(f"/api/sessions/{shared}/exposure").text[:200])

    # =========================================================================
    # GitHub accounts: the same disclosure, folded into the same Exposure
    # =========================================================================
    #
    # A stored SSH target and a workspace's linked GitHub account are both
    # bearer credentials by the same reasoning (see exposure.py's module
    # docstring), and the design here is one combined `Exposure` rather than a
    # second warning: a caller who acknowledged their SSH key being exposed but
    # was never asked about their GitHub token reaching the same audience would
    # not actually be informed. Fresh users are used throughout so a GitHub
    # account is provably the *only* thing that can be at stake for them — any
    # of these checks passing for some other reason (a stray stored system, a
    # stale acknowledgement) would be a bug this section exists to catch.
    login(c, "admin")
    erin = make_user(c, "erin")
    frank = make_user(c, "frank")
    george = make_user(c, "george")
    GH_TOKEN = "ghp_" + "y" * 36

    # --- a session with only a GitHub account at stake, no SSH targets -----
    as_user(c, "erin")
    r = c.post("/api/github-accounts", json={"label": "Erin's GitHub", "token": GH_TOKEN})
    check("erin can store a GitHub account", r.status_code == 201, r.text[:200])
    erin_gh_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/workspaces", json={"name": "erin-ws", "path": "erin-ws"})
    check("(setup) erin's workspace", r.status_code == 201, r.text[:200])
    erin_ws_id = r.json()["id"] if r.status_code == 201 else None

    r = c.patch(f"/api/workspaces/{erin_ws_id}", json={"github_account_id": erin_gh_id})
    check("(setup) erin links her workspace to her own account",
          r.status_code == 200, r.text[:200])

    gh_only = c.post("/api/sessions", json={
        "provider": "claude", "title": "erin's github-only session",
        "workspace_id": erin_ws_id, "approval_mode": "bypass",
    }).json()["id"]
    r = c.patch(f"/api/sessions/{gh_only}", json={"shared_user_ids": [frank]})
    check("(setup) erin shares the session with frank", r.status_code == 200, r.text[:200])

    r = c.get(f"/api/sessions/{gh_only}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("erin has no stored systems of her own",
          view.get("systems") == [], str(view.get("systems")))
    check("but her linked GitHub account is named",
          [a["id"] for a in view.get("github_accounts", [])] == [erin_gh_id],
          str(view.get("github_accounts")))
    check("no token or repo list rides along with it — just id and label",
          all(set(a) == {"id", "label"} for a in view.get("github_accounts", [])),
          str(view.get("github_accounts")))
    check("a GitHub account alone is enough to put something at stake",
          view.get("at_stake") is True, str(view)[:200])
    check("so the gate is armed with zero SSH systems in play",
          view.get("needs_acknowledgement") is True, str(view)[:200])

    r = c.post(f"/api/sessions/{gh_only}/prompt", json={"prompt": "look at the repo"})
    check("the first turn is refused for the GitHub account alone",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    check("the refusal names it as a GitHub account rather than 'stored systems'",
          "GitHub account" in r.text and "stored systems" not in r.text, r.text[:300])
    check("and still names who would read it", "frank" in r.text, r.text[:300])

    r = c.post(f"/api/sessions/{gh_only}/exposure/ack", json={"viewer_ids": [frank]})
    check("acknowledging the GitHub-only exposure is recorded",
          r.status_code == 200 and r.json()["acknowledged"] is True,
          f"{r.status_code} {r.text[:200]}")

    r = c.post(f"/api/sessions/{gh_only}/prompt", json={"prompt": "look at the repo now"})
    check("and the turn then goes through", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    gh_run = r.json()["id"] if r.status_code == 202 else None
    settle(c, gh_run)
    notes = exposure_events(c, gh_only)
    check("the transcript records the GitHub account was used",
          len(notes) == 1 and notes[0][1].get("github_account", {}).get("id") == erin_gh_id,
          str(notes)[:300])
    check("and the note text names it by label",
          len(notes) == 1 and "Erin's GitHub" in (notes[0][0]["text"] or ""),
          str(notes[:1])[:300])

    # --- one acknowledgement covers an SSH target and a GitHub account -----
    # Erin now also stores a system. The gate has already been armed and
    # cleared once for her GitHub account (above, in a different session) —
    # this checks that a *fresh* session mixing both credential kinds is
    # covered by a single acknowledgement, not one per kind.
    as_user(c, "erin")
    r = c.post("/api/targets", json={
        "name": "Erin Box", "hostname": "10.9.9.20", "username": "erin",
        "auth_type": "key", "private_key": PRIVATE_KEY,
    })
    check("erin can also store a system", r.status_code == 201, r.text[:200])

    combo_sess = c.post("/api/sessions", json={
        "provider": "claude", "title": "erin's combined session",
        "workspace_id": erin_ws_id, "approval_mode": "bypass",
    }).json()["id"]
    r = c.patch(f"/api/sessions/{combo_sess}", json={"shared_user_ids": [frank]})
    check("(setup) shared with frank", r.status_code == 200, r.text[:200])

    r = c.get(f"/api/sessions/{combo_sess}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("both the SSH system and the GitHub account are named together",
          [s["slug"] for s in view.get("systems", [])] == ["erin-box"]
          and [a["id"] for a in view.get("github_accounts", [])] == [erin_gh_id],
          str(view)[:300])
    check("one Exposure covers both", view.get("at_stake") is True, str(view)[:200])

    r = c.post(f"/api/sessions/{combo_sess}/prompt", json={"prompt": "combined"})
    check("the first turn is refused with both credential kinds at stake",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    check("and the refusal names both kinds together",
          "stored systems" in r.text and "GitHub account" in r.text, r.text[:300])

    r = c.post(f"/api/sessions/{combo_sess}/exposure/ack", json={"viewer_ids": [frank]})
    check("a single acknowledgement is recorded for both",
          r.status_code == 200 and r.json()["acknowledged"] is True,
          f"{r.status_code} {r.text[:200]}")

    r = c.post(f"/api/sessions/{combo_sess}/prompt", json={"prompt": "combined now"})
    check("and it covers both — no second prompt is needed for the GitHub account",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    combo_run = r.json()["id"] if r.status_code == 202 else None
    settle(c, combo_run)
    notes = exposure_events(c, combo_sess)
    check("the transcript records both credential kinds used together in one note",
          len(notes) == 1
          and [s["slug"] for s in notes[0][1]["systems"]] == ["erin-box"]
          and notes[0][1].get("github_account", {}).get("id") == erin_gh_id,
          str(notes)[:300])

    # --- disclosed via the requester's own access, not the workspace's owner -
    # George owns a GitHub account and a workspace linked to it. Frank is
    # granted 'use' on *both* the workspace and the account, so a turn of
    # frank's own would actually reach george's account — and that is what
    # frank's own exposure has to disclose. Erin can read the very same
    # session but was never granted anything on george's account (or his
    # workspace), so nothing is disclosed to her: the check tracks the
    # caller's own access to the credential, not merely being able to read
    # the session, and not the workspace's owner's access either.
    as_user(c, "george")
    r = c.post("/api/github-accounts", json={"label": "George's GitHub", "token": GH_TOKEN})
    george_gh_id = r.json()["id"]
    r = c.post("/api/workspaces", json={"name": "george-ws", "path": "george-ws"})
    george_ws_id = r.json()["id"]
    c.patch(f"/api/workspaces/{george_ws_id}", json={"github_account_id": george_gh_id})
    c.patch(f"/api/workspaces/{george_ws_id}",
            json={"grants": [{"user_id": frank, "level": "use"}]})
    c.patch(f"/api/github-accounts/{george_gh_id}",
            json={"grants": [{"user_id": frank, "level": "use"}]})

    requester_sess = c.post("/api/sessions", json={
        "provider": "claude", "title": "george's linked-account session",
        "workspace_id": george_ws_id, "approval_mode": "bypass",
    }).json()["id"]
    c.patch(f"/api/sessions/{requester_sess}", json={"shared_user_ids": [frank, erin]})

    as_user(c, "frank")
    r = c.get(f"/api/sessions/{requester_sess}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("frank, granted use on both the workspace and the account, sees it disclosed",
          [a["id"] for a in view.get("github_accounts", [])] == [george_gh_id],
          str(view.get("github_accounts")))

    as_user(c, "erin")
    r = c.get(f"/api/sessions/{requester_sess}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("erin can read the same session, but was never granted george's "
          "account, so nothing of george's is disclosed to her",
          view.get("github_accounts") == [], str(view.get("github_accounts")))
    # Not "nothing at all is at stake for her" — by this point erin also has
    # her own stored SSH system (added above), and that system is reachable
    # from *any* session she can prompt in, this one included; visible_targets
    # is deliberately global, not scoped to a workspace, the same way it is for
    # every other member in this file. What is under test here is specifically
    # that george's GitHub account contributes nothing to her exposure.
    check("her own unrelated stored system is what is at stake for her here, "
          "not george's account",
          [s["slug"] for s in view.get("systems", [])] == ["erin-box"]
          and view.get("at_stake") is True,
          str(view)[:200])

    # --- the confused deputy, exactly as the module's docstring describes ---
    # Frank creates his own room and speaks first, so his message is genuinely
    # already in the transcript before george — who owns the credential this
    # scenario is about — is even added, let alone sends his own first prompt.
    # A fresh workspace/account pair with no grants to frank at all keeps
    # frank's own first message free of any exposure gate of its own, so the
    # only thing under test is whether george's gate fires because of frank's
    # already-present message — not merely because "some other viewer exists"
    # in the abstract.
    as_user(c, "george")
    r = c.post("/api/github-accounts", json={"label": "George's Deputy GitHub", "token": GH_TOKEN})
    deputy_gh_id = r.json()["id"]
    r = c.post("/api/workspaces", json={"name": "george-deputy-ws", "path": "george-deputy-ws"})
    deputy_ws_id = r.json()["id"]
    c.patch(f"/api/workspaces/{deputy_ws_id}", json={"github_account_id": deputy_gh_id})

    deputy_sess = c.post("/api/sessions", json={
        "provider": "claude", "title": "george's room",
        "workspace_id": deputy_ws_id, "approval_mode": "bypass",
    }).json()["id"]
    r = c.patch(f"/api/sessions/{deputy_sess}", json={"shared_user_ids": [frank]})
    check("(setup) george shares the room with frank before anyone has spoken",
          r.status_code == 200, r.text[:200])

    as_user(c, "frank")
    r = c.post(f"/api/sessions/{deputy_sess}/prompt",
               json={"prompt": "can you check the production repo for me?"})
    check("(setup) frank speaks first — no exposure of george's credential yet, "
          "so nothing holds frank's own message back",
          r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    settle(c, r.json()["id"] if r.status_code == 202 else None)

    as_user(c, "george")
    transcript_before = c.get(f"/api/sessions/{deputy_sess}/transcript").json()
    check("frank's message is genuinely already in the transcript george reads",
          any("check the production repo" in (run.get("prompt") or "")
              for run in transcript_before.get("runs", [])),
          str(transcript_before)[:300])

    r = c.get(f"/api/sessions/{deputy_sess}/exposure")
    view = r.json() if r.status_code == 200 else {}
    check("george's own GitHub account is what would be exposed here",
          [a["id"] for a in view.get("github_accounts", [])] == [deputy_gh_id],
          str(view)[:200])
    check("the gate is armed before george's own first prompt into this room",
          view.get("needs_acknowledgement") is True, str(view)[:200])

    r = c.post(f"/api/sessions/{deputy_sess}/prompt",
               json={"prompt": "sure, here's what the repo has"})
    check("george's first turn is refused — this is the exact shape the "
          "module's docstring names: an earlier viewer's message sits in the "
          "transcript his credential-bearing turn would now run in front of",
          r.status_code == 428, f"{r.status_code} {r.text[:200]}")
    check("the refusal names frank, the earlier viewer, specifically",
          "frank" in r.text, r.text[:300])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
