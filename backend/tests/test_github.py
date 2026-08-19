"""GitHub accounts: the fourth owner/grant model, and the clone-to-workspace flow.

Written as the mirror of `test_targets.py` and `test_workspaces.py`, because
the rule is deliberately the same one, reaffirmed a fourth time in
`access.github_account_level_for`: a GitHub account belongs to whoever added
it, is invisible to everyone else until they are named, and **an
administrator who was not named gets 404** — not 403.

Three things are specific to this feature and are the reason it exists as a
file of its own:

* the clone-to-workspace endpoint actually runs a real `git clone` — of a
  local, offline bare repository substituted in place of the github.com URL —
  with this feature's real credential-helper plumbing wired into the
  subprocess's environment exactly as production does. The resulting
  `.git/config` is read back and grepped, so "the token never persists there"
  is a checked fact rather than a claim about code nobody ran.
* the credential helper `github_creds.py` writes is itself invoked through a
  real `git credential fill`, scoped to `https://github.com` only, and a
  different host is checked to get nothing back.
* the run-time rule from `runner.py`: a turn runs as whoever asked for it, so
  a shared session must not lend out its owner's workspace's GitHub account —
  checked the same way `test_workspaces.py` checks the workspace-access rule
  itself, by driving a real turn and reading why it failed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

for _stale in ("./test-github.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-github.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-github-test-ws-"))

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


from app import agent_env, github_creds  # noqa: E402
from app.access import LEVELS, github_account_level_for  # noqa: E402
from app.models import GithubAccount, GithubAccountAccess, User as UserModel  # noqa: E402

# --- the rule itself, before any HTTP ---------------------------------------
_owner = UserModel(id=1, username="owner")
_other = UserModel(id=2, username="other")
_admin = UserModel(id=3, username="admin2", is_admin=True)
_acct = GithubAccount(id=1, label="a", owner_id=1, grants=[])
check("the owner owns it", github_account_level_for(_acct, _owner) == "owner")
check("a stranger gets nothing", github_account_level_for(_acct, _other) is None)
check("an administrator gets nothing implicitly",
      github_account_level_for(_acct, _admin) is None, str(github_account_level_for(_acct, _admin)))
check("nobody at all gets nothing", github_account_level_for(_acct, None) is None)
_acct.grants = [GithubAccountAccess(github_account_id=1, user_id=2, level="manage")]
check("a grant is honoured at its level", github_account_level_for(_acct, _other) == "manage")
_acct.grants = [GithubAccountAccess(github_account_id=1, user_id=2, level="nonsense")]
check("an unknown level degrades to 'use' rather than widening",
      github_account_level_for(_acct, _other) == "use")
check("the levels are the same two as everywhere else", LEVELS == ("use", "manage"))

# --- github_creds.py: the materialised credential, off the network ---------
_fake_acct = GithubAccount(id=99, label="probe", token_enc=None)
from app.crypto import encrypt  # noqa: E402

TOKEN = "ghp_" + "x" * 36
_fake_acct.token_enc = encrypt(TOKEN)

ctx = github_creds.prepare(_fake_acct)
check("prepare() returns a context for a readable token", ctx is not None)
if ctx is not None:
    with open(os.path.join(ctx.root, "credential-helper"), encoding="utf-8") as fh:
        helper_text = fh.read()
    check("the token is in the helper script", TOKEN in helper_text)
    check("the helper only answers 'get'",
          'case "$1" in' in helper_text and "get)" in helper_text, helper_text[:200])
    check("GIT_CONFIG_GLOBAL points into the run's own directory",
          ctx.env.get("GIT_CONFIG_GLOBAL", "").startswith(ctx.root), str(ctx.env))
    check("nothing is asked over a terminal", ctx.env.get("GIT_TERMINAL_PROMPT") == "0")

    # `git` execing this helper script through a real credential-fill round
    # trip only runs on POSIX: on Windows, `git`'s bundled MSYS shell mangles
    # the absolute (backslash) helper path before it ever reaches a shell that
    # could interpret its shebang line — a platform quirk of this local
    # sanity check, not of the code under test, and orthogonal to the gate,
    # which runs in a Linux container where the path and the shebang are both
    # native. Everything above (token in the file, github.com-only scoping,
    # cleanup) is checked on every platform; only this one exec round trip is
    # POSIX-only.
    have_git = os.name != "nt" and subprocess.run(
        ["git", "--version"], capture_output=True
    ).returncode == 0
    if have_git:
        def credential_fill(host: str) -> str:
            proc = subprocess.run(
                ["git", "-c", f"include.path={ctx.env['GIT_CONFIG_GLOBAL']}",
                 "credential", "fill"],
                input=f"protocol=https\nhost={host}\n\n".encode(),
                capture_output=True,
                env={**os.environ, **ctx.env},
            )
            return proc.stdout.decode("utf-8", "replace")

        out = credential_fill("github.com")
        check("git actually hands back the token for github.com",
              f"password={TOKEN}" in out, out[:200])
        check("and the username github expects for a token",
              "username=x-access-token" in out, out[:200])

        other = credential_fill("example.com")
        check("a different host gets nothing at all — the helper is github.com-scoped",
              TOKEN not in other, other[:200])

    ctx.cleanup()
    check("cleanup removes the run's credential directory", not os.path.exists(ctx.root))

bad_acct = GithubAccount(id=100, label="unreadable", token_enc=None)
check("prepare() returns None rather than raising for an account with no token",
      github_creds.prepare(bad_acct) is None)


# --- over HTTP ---------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def make_user(client, username, *, admin=False):
    client.post("/api/users", json={
        "username": username, "password": f"{username}password1",
        "is_admin": admin, "must_change_password": False,
    })
    return {u["username"]: u["id"] for u in client.get("/api/users").json()}[username]


def login(client, username, password=None):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={
        "username": username, "password": password or f"{username}password1",
    })
    assert r.status_code == 200, r.text
    return r


def settle(client, run_id, seconds=45):
    row = {}
    for _ in range(seconds * 4):
        row = client.get(f"/api/runs/{run_id}").json()
        if row.get("status") not in ("queued", "running"):
            return row
        time.sleep(0.25)
    return row


# A local, offline stand-in for github.com: an empty bare repo, cloned in place
# of the https:// URL by the monkeypatch below. This is what lets the
# clone-to-workspace endpoint be exercised through a real `git clone` — proving
# the actual credential wiring, not a mock of it — with no network reachable
# from this test at all.
FIXTURE_BARE = tempfile.mkdtemp(prefix="aiops-github-fixture-")
subprocess.run(["git", "init", "--bare", "-q", FIXTURE_BARE], check=True)

_real_spawn = agent_env.spawn
_last_clone_env: dict = {}


async def _patched_spawn(argv, **kwargs):
    """Redirect `git clone -- <https://github.com/...> <path>` at the fixture.

    Everything else about the call — the environment carrying the credential
    helper, the working directory, cleanup — passes through to the real
    `agent_env.spawn` unchanged, so the only thing being faked is network
    access to github.com itself.
    """
    argv = list(argv)
    if len(argv) >= 5 and argv[0] == "git" and argv[1] == "clone":
        _last_clone_env.clear()
        _last_clone_env.update(kwargs.get("env") or {})
        check("the real argv handed to git carries no embedded credential",
              argv[3] == "https://github.com/octocat/Hello-World.git", argv[3])
        argv[3] = FIXTURE_BARE
    return await _real_spawn(argv, **kwargs)


import app.routers.workspaces as workspaces_router  # noqa: E402

workspaces_router.agent_env.spawn = _patched_spawn


with TestClient(app) as c:
    login(c, "admin", "devpassword123")
    walt = make_user(c, "walt")
    otheradmin = make_user(c, "otheradmin", admin=True)

    # --- CRUD, owner/use/manage/admin/stranger --------------------------
    login(c, "walt")
    r = c.post("/api/github-accounts", json={"label": "Walt's GitHub", "token": TOKEN})
    check("creating a GitHub account succeeds", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    acct = r.json() if r.status_code == 201 else {}
    acct_id = acct.get("id")
    check("it belongs to whoever created it",
          acct.get("owner_id") == walt and acct.get("my_level") == "owner", str(acct))
    # Anchored on the opening quote, the same way test_targets.py checks
    # `"private_key"`: `has_token` legitimately contains the letters t-o-k-e-n,
    # and an unanchored search would flag the very boolean indicator that
    # proves no secret field is present.
    check("the token is never returned",
          '"token"' not in c.get("/api/github-accounts").text and TOKEN not in acct.get("label", ""))
    check("only whether one is stored is reported", acct.get("has_token") is True)

    login(c, "admin", "devpassword123")
    r = c.get("/api/github-accounts")
    check("an admin does NOT see a GitHub account somebody else added",
          r.status_code == 200 and all(a["id"] != acct_id for a in r.json()), r.text[:200])
    for label, resp in (
        ("patch", c.patch(f"/api/github-accounts/{acct_id}", json={"label": "mine now"})),
        ("delete", c.delete(f"/api/github-accounts/{acct_id}")),
    ):
        check(f"an administrator with no grant gets 404 from {label}",
              resp.status_code == 404, str(resp.status_code))

    login(c, "otheradmin")
    check("an unrelated user (even an admin) gets 404, not 403",
          c.patch(f"/api/github-accounts/{acct_id}", json={"label": "x"}).status_code == 404)

    login(c, "walt")
    r = c.patch(f"/api/github-accounts/{acct_id}",
                json={"grants": [{"user_id": otheradmin, "level": "use"}]})
    check("the owner can share it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    login(c, "otheradmin")
    r = c.get("/api/github-accounts")
    shared = [a for a in r.json() if a["id"] == acct_id]
    check("a shared account appears for the grantee at its granted level",
          len(shared) == 1 and shared[0]["my_level"] == "use", r.text[:200])
    r = c.patch(f"/api/github-accounts/{acct_id}", json={"label": "renamed"})
    check("but 'use' cannot change it", r.status_code == 403, str(r.status_code))
    r = c.patch(f"/api/github-accounts/{acct_id}",
                json={"grants": [{"user_id": walt, "level": "manage"}]})
    check("and 'use' cannot grant it onward", r.status_code == 403, str(r.status_code))

    login(c, "walt")
    c.patch(f"/api/github-accounts/{acct_id}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    login(c, "otheradmin")
    r = c.patch(f"/api/github-accounts/{acct_id}", json={"label": "managed"})
    check("'manage' can change it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/github-accounts/{acct_id}", json={"owner_id": otheradmin})
    check("but only the owner can hand it over", r.status_code == 403, str(r.status_code))

    # --- reject anything that is not a github.com repo ------------------
    login(c, "walt")
    for bad in ("git@github.com:owner/repo.git", "https://gitlab.com/owner/repo",
                "ssh://github.com/owner/repo", "not a repo at all", ""):
        r = c.post("/api/workspaces/from-github",
                    json={"github_account_id": acct_id, "repo": bad})
        check(f"rejected non-github.com repo spec {bad!r}",
              r.status_code in (400, 422), f"{r.status_code} {r.text[:200]}")

    # --- an account the caller cannot see -------------------------------
    r = c.post("/api/workspaces/from-github",
               json={"github_account_id": 999999, "repo": "octocat/Hello-World"})
    check("an unknown account id is 404", r.status_code == 404, str(r.status_code))
    login(c, "otheradmin")
    # otheradmin currently holds 'manage' on acct via the grant above, so give
    # it back to a stranger scenario using a second, ungranted account.
    login(c, "walt")
    r = c.post("/api/github-accounts", json={"label": "private one", "token": TOKEN})
    private_acct_id = r.json()["id"]
    login(c, "otheradmin")
    r = c.post("/api/workspaces/from-github",
               json={"github_account_id": private_acct_id, "repo": "octocat/Hello-World"})
    check("a GitHub account not shared with the caller is 404, not 403",
          r.status_code == 404, str(r.status_code))

    # --- the real clone, redirected to a local fixture ------------------
    login(c, "walt")
    r = c.post("/api/workspaces/from-github",
               json={"github_account_id": acct_id, "repo": "octocat/Hello-World"})
    check("cloning succeeds", r.status_code == 201, f"{r.status_code} {r.text[:500]}")
    ws = r.json() if r.status_code == 201 else {}
    check("the workspace is owned by whoever asked for the clone, not an admin",
          ws.get("owner_id") == walt, str(ws)[:200])
    check("the workspace is linked to the GitHub account used to clone it",
          ws.get("github_account_id") == acct_id, str(ws)[:200])
    check("the workspace name defaults to the repo's own name",
          ws.get("name") == "Hello-World", str(ws.get("name")))

    ws_path = ws.get("path", "")
    git_config_path = os.path.join(ws_path, ".git", "config")
    check("the clone actually landed on disk", os.path.isfile(git_config_path), git_config_path)
    if os.path.isfile(git_config_path):
        with open(git_config_path, encoding="utf-8", errors="replace") as fh:
            cfg = fh.read()
        # The property this whole endpoint exists to guarantee.
        check("the token never appears in .git/config", TOKEN not in cfg, cfg[:300])
        check("no credential is embedded in the remote URL either",
              "x-access-token" not in cfg and "@github.com" not in cfg, cfg[:300])

    _clone_gitconfig = _last_clone_env.get("GIT_CONFIG_GLOBAL", "")
    check("the clone actually received a per-run GIT_CONFIG_GLOBAL", bool(_clone_gitconfig))
    if _clone_gitconfig:
        _clone_cred_dir = os.path.dirname(_clone_gitconfig)
        check("the credential directory handed to the clone process is cleaned up afterwards",
              not os.path.exists(_clone_cred_dir), _clone_cred_dir)

    # --- collision ---------------------------------------------------------
    r = c.post("/api/workspaces/from-github",
               json={"github_account_id": acct_id, "repo": "octocat/Hello-World",
                     "name": "Hello-World-2"})
    check("cloning the same repo again collides on the directory",
          r.status_code == 409, f"{r.status_code} {r.text[:300]}")

    r = c.post("/api/workspaces/from-github",
               json={"github_account_id": acct_id, "repo": "octocat/Hello-World"})
    check("and on the workspace name too",
          r.status_code == 409, f"{r.status_code} {r.text[:300]}")

    # --- linking / unlinking an existing workspace --------------------------
    login(c, "admin", "devpassword123")
    r = c.post("/api/workspaces", json={"name": "plain-ws", "path": "plain-ws"})
    plain_ws = r.json()
    r = c.patch(f"/api/workspaces/{plain_ws['id']}", json={"github_account_id": acct_id})
    check("linking to a GitHub account you cannot use is refused",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    login(c, "walt")
    c.patch(f"/api/github-accounts/{acct_id}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    login(c, "admin", "devpassword123")
    r = c.patch(f"/api/workspaces/{plain_ws['id']}", json={"github_account_id": acct_id})
    check("but linking to one you cannot use is still refused for the admin too",
          r.status_code == 400, str(r.status_code))

    login(c, "walt")
    r = c.patch(f"/api/workspaces/{ws['id']}", json={"github_account_id": None})
    check("the workspace owner can unlink the account", r.status_code == 200
          and r.json()["github_account_id"] is None, f"{r.status_code} {r.text[:200]}")

    # --- deleting the account unlinks any workspace using it, not orphans it
    r = c.patch(f"/api/workspaces/{ws['id']}", json={"github_account_id": acct_id})
    check("(setup) relink for the delete check", r.status_code == 200, r.text[:200])
    r = c.delete(f"/api/github-accounts/{private_acct_id}")
    check("deleting an unused GitHub account succeeds", r.status_code == 204, str(r.status_code))
    r = c.delete(f"/api/github-accounts/{acct_id}")
    check("deleting a GitHub account still linked to a workspace succeeds",
          r.status_code == 204, str(r.status_code))
    r = c.get(f"/api/workspaces")
    still = [w for w in r.json() if w["id"] == ws["id"]]
    check("and the workspace survives it, simply unlinked",
          still and still[0]["github_account_id"] is None, str(still[:1])[:200])

    # --- run time: the requester, not the session's owner -------------------
    login(c, "walt")
    r = c.post("/api/github-accounts", json={"label": "run-time acct", "token": TOKEN})
    run_acct_id = r.json()["id"]
    r = c.post("/api/workspaces", json={"name": "gh-run-ws", "path": "gh-run-ws"})
    run_ws = r.json()
    r = c.patch(f"/api/workspaces/{run_ws['id']}", json={"github_account_id": run_acct_id})
    check("(setup) workspace linked to walt's own account", r.status_code == 200, r.text[:200])

    sess = c.post("/api/sessions", json={
        "provider": "claude", "workspace_id": run_ws["id"], "approval_mode": "bypass",
    }).json()
    mine = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "the owner's turn"})
    check("the owner can send a turn into their own linked workspace",
          mine.status_code == 202, f"{mine.status_code} {mine.text[:200]}")
    owner_row = settle(c, mine.json()["id"])
    # No `claude` on PATH in the test environment, so this fails — but it must
    # fail for *that* reason, not a GitHub-access one, which is what proves the
    # check let the owner through.
    check("and it is not refused for a GitHub-account reason",
          "GitHub account" not in (owner_row.get("error") or ""),
          str(owner_row.get("error"))[:200])

    c.patch(f"/api/sessions/{sess['id']}", json={"shared_user_ids": [otheradmin]})
    # otheradmin needs the *workspace* itself before the GitHub-account check
    # is even reached — runner.py checks workspace access first, exactly as
    # test_workspaces.py's run-time section does, and this test is isolating
    # the GitHub-account rule specifically, not re-proving the workspace one.
    c.patch(f"/api/workspaces/{run_ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "use"}]})
    login(c, "otheradmin")
    theirs = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "a guest's turn"})
    check("a sharee can still queue a turn into a session they were let into",
          theirs.status_code == 202, f"{theirs.status_code} {theirs.text[:200]}")
    guest_row = settle(c, theirs.json()["id"])
    check("but the turn fails, because the GitHub account is not theirs",
          guest_row.get("status") == "failed", str(guest_row)[:200])
    check("and it says so, naming the workspace and the GitHub account",
          "gh-run-ws" in (guest_row.get("error") or "")
          and "GitHub account" in (guest_row.get("error") or ""),
          str(guest_row.get("error"))[:300])

    login(c, "walt")
    c.patch(f"/api/github-accounts/{run_acct_id}",
            json={"grants": [{"user_id": otheradmin, "level": "use"}]})
    login(c, "otheradmin")
    allowed = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "a granted turn"})
    granted_row = settle(c, allowed.json()["id"])
    check("a granted requester's turn is not refused for a GitHub-account reason",
          "GitHub account" not in (granted_row.get("error") or ""),
          str(granted_row.get("error"))[:200])

    # --- offboarding: hand-on or refuse, matching Target/Node/Workspace -----
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{walt}")
    check("deleting a user who owns a GitHub account nobody else can manage is refused",
          r.status_code == 409, f"{r.status_code} {r.text[:300]}")
    check("and the refusal names what is stranded",
          "run-time acct" in r.text or "gh-run-ws" in r.text or "walt" in r.text, r.text[:300])

    login(c, "walt")
    c.patch(f"/api/github-accounts/{run_acct_id}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    # walt also still owns the workspaces from the clone and run-time sections
    # above, with nobody else able to manage them — that would strand the
    # delete too, for a reason this test is not about, so they get the same
    # treatment.
    c.patch(f"/api/workspaces/{run_ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    c.patch(f"/api/workspaces/{ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{walt}")
    check("deleting the owner succeeds once every account has a manager to inherit it",
          r.status_code == 204, f"{r.status_code} {r.text[:300]}")

workspaces_router.agent_env.spawn = _real_spawn

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
