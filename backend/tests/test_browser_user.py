"""The user the browser runs as, and what that user cannot reach.

This suite exists for the same reason test_isolation.py does — a boundary that
was measured rather than assumed — one layer further out.

The agent's browser was shipped running as the agent: uid 1001, group `node`.
Three facts made that a real exposure rather than an untidy one. A run's stored
SSH private keys are decrypted to disk at 0640 owned by the app and grouped to
`node`, because `ssh` runs as the agent and has to load them. The run's stored
passwords are in AIOPS_SSHPASS_* in the agent's environment, and its relay token
in AIOPS_RELAY_TOKEN, for the same reason. And Chromium's own sandbox is off in
this container, because the unprivileged user namespace it needs is blocked by
Docker's default seccomp profile. So a renderer exploit on a hostile page —
which is the thing a browser is for, and the only place in AIOps where content
nobody vetted is executed at all — was code execution beside all of it.

The fix is a third user: its own uid, its own group, in neither the app's group
nor the agent's, reached through the same setuid helper that already starts
agents. What is asserted below is the half that can be asserted anywhere (the
environment sweep, and that the three descriptions of it have not drifted
apart), and then, where a real helper exists, the half that can only be
measured: that the browser user is genuinely refused a key materialised for a
real run, and that the agent can still read what the browser produced.

Run inside the image for the second half. Outside it, those checks skip, and
say so.
"""

import asyncio
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-browser-user.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")

from app import agent_env, browsing, ssh_targets  # noqa: E402
from app.bridge import mcp_browser as mb  # noqa: E402
from app.config import settings  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.models import Target  # noqa: E402

POSIX = os.name == "posix"
ROOT = os.path.dirname(os.getcwd())
CSOURCE = os.path.join(ROOT, "deploy", "runas", "aiops-runas.c")
DOCKERFILE = os.path.join(ROOT, "Dockerfile")

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def skip(label, why):
    print(f"[skip] {label} — {why}")


# =====================================================================
# 1. The environment the browser stack starts from
# =====================================================================
print("\n--- what the browser inherits ---")

# Named as a real run has them. AIOPS_SSHPASS_<SLUG> is a stored system's
# password verbatim; AIOPS_ASKPASS_<SLUG> is the path to a program that prints
# a key's passphrase, which is worth exactly as much as the passphrase.
RUN_ENVIRONMENT = {
    "AIOPS_SSHPASS_SONARR": "the-stored-password",
    "AIOPS_ASKPASS_BACKUP": "/tmp/aiops-ssh-xyz/bin/askpass-backup",
    "AIOPS_RELAY_TOKEN": "relay-token-for-this-run",
    "AIOPS_RELAY_ADDR": "127.0.0.1:41234",
    "AIOPS_APPROVAL_TOKEN": "loopback-token-for-this-run",
    "AIOPS_SSH_CONFIG": "/tmp/aiops-ssh-xyz/config",
    "AIOPS_SECRET_KEY": "decrypts-every-stored-credential",
    "AIOPS_DATABASE_URL": "postgresql://aiops:aiops@db:5432/aiops",
    "AIOPS_WORKSPACE_ROOT": "/workspaces",
    "POSTGRES_PASSWORD": "postgres-password-in-the-environment",
    "PGPASSWORD": "another-postgres-password",
    "DATABASE_URL": "postgresql://someone:secret@db:5432/aiops",
    "SECRET_KEY": "bare-name-too",
    "PATH": "/usr/local/bin:/usr/bin",
    "HOME": "/home/node",
    "PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright",
    "LANG": "C.UTF-8",
}

swept = mb.browser_environ(RUN_ENVIRONMENT)

check("a stored system's password does not reach the browser",
      "AIOPS_SSHPASS_SONARR" not in swept, str(sorted(swept)))
check("nor does the helper that prints a key's passphrase",
      "AIOPS_ASKPASS_BACKUP" not in swept, str(sorted(swept)))
check("nor the relay token, which opens streams through a node",
      "AIOPS_RELAY_TOKEN" not in swept and "AIOPS_RELAY_ADDR" not in swept)
check("nor the token the bridge answers the app's loopback API with",
      "AIOPS_APPROVAL_TOKEN" not in swept)
check("nor where this run's ssh materials are", "AIOPS_SSH_CONFIG" not in swept)
check("nor anything the agent is already denied",
      not any(name in swept for name in
              ("AIOPS_SECRET_KEY", "AIOPS_DATABASE_URL", "POSTGRES_PASSWORD",
               "PGPASSWORD", "DATABASE_URL", "SECRET_KEY")),
      str(sorted(swept)))
# The agent keeps this one deliberately (agent_env._ALLOWED_AIOPS). A browser
# has no workspace and no business knowing where one is, so the sweep here is
# strictly the wider of the two.
check("not even the one AIOPS_ variable an agent is allowed to keep",
      "AIOPS_WORKSPACE_ROOT" not in swept)
check("what the browser actually needs survives",
      swept.get("PATH") and swept.get("PLAYWRIGHT_BROWSERS_PATH") == "/opt/playwright"
      and swept.get("LANG") == "C.UTF-8", str(sorted(swept)))
check("and nothing that was not blocked was dropped",
      set(RUN_ENVIRONMENT) - set(swept) == {
          "AIOPS_SSHPASS_SONARR", "AIOPS_ASKPASS_BACKUP", "AIOPS_RELAY_TOKEN",
          "AIOPS_RELAY_ADDR", "AIOPS_APPROVAL_TOKEN", "AIOPS_SSH_CONFIG",
          "AIOPS_SECRET_KEY", "AIOPS_DATABASE_URL", "AIOPS_WORKSPACE_ROOT",
          "POSTGRES_PASSWORD", "PGPASSWORD", "DATABASE_URL", "SECRET_KEY",
      },
      str(sorted(set(RUN_ENVIRONMENT) - set(swept))))
check("the sweep is at least as wide as the agent's own",
      all(mb.blocked_in_browser(name) for name in agent_env._BLOCKED_NAMES)
      and all(mb.blocked_in_browser(prefix + "ANYTHING")
              for prefix in agent_env._BLOCKED_PREFIXES))


# =====================================================================
# 2. Three copies of one list, and no drift between them
# =====================================================================
print("\n--- the sweep is written down three times ---")

# It has to be: Python cannot sweep an environment for a process the setuid
# helper is about to inherit, and the helper cannot be trusted to exist in a
# checkout. So each does it, and this is what stops them from disagreeing.
source = open(CSOURCE, encoding="utf-8").read() if os.path.exists(CSOURCE) else ""
check("the helper's source is where this expects it", bool(source), CSOURCE)

if source:
    prefixes = re.search(r"prefixes\[\] = \{([^}]*)\}", source)
    names = re.search(r"names\[\] = \{([^}]*)\}", source)
    c_prefixes = tuple(re.findall(r'"([^"]+)"', prefixes.group(1))) if prefixes else ()
    c_names = frozenset(re.findall(r'"([^"]+)"', names.group(1))) if names else frozenset()

    check("the helper blocks the same prefixes Python does",
          c_prefixes == mb._BLOCKED_PREFIXES, f"{c_prefixes} vs {mb._BLOCKED_PREFIXES}")
    check("and the same bare names", c_names == mb._BLOCKED_NAMES,
          f"{sorted(c_names)} vs {sorted(mb._BLOCKED_NAMES)}")
    check("and Python's list is agent_env's, so all three move together",
          mb._BLOCKED_PREFIXES == agent_env._BLOCKED_PREFIXES
          and mb._BLOCKED_NAMES == agent_env._BLOCKED_NAMES)
    check("the helper only ever moves downwards, never to root",
          "built with a root target" in source)
    check("the browser user is refused re-entry into the helper",
          "AIOPS_APP_UID && getuid() != (uid_t)AIOPS_AGENT_UID" in source, "")
    check("a browser cannot outlive the bridge answerable for it",
          "PR_SET_PDEATHSIG" in source)
    check("and cancelling a run still reaches it, though it is not the agent",
          "AIOPS_BROWSER_UID" in source and "owned_by_isolated_user" in source)

dockerfile = open(DOCKERFILE, encoding="utf-8").read() if os.path.exists(DOCKERFILE) else ""
if dockerfile:
    check("the image gives the browser a group of its own, not the agent's",
          "groupadd --gid ${BROWSER_GID} aiops-browser" in dockerfile
          and "--gid ${BROWSER_GID}" in dockerfile
          and "--gid node --no-create-home" in dockerfile,
          "the agent keeps `node`; the browser must not be given it")
    check("and Playwright is pointed at the shim rather than its own node",
          "PLAYWRIGHT_NODEJS_PATH" in open(
              os.path.join(os.getcwd(), "app", "bridge", "mcp_browser.py"), encoding="utf-8"
          ).read() and "browser-node" in dockerfile)
else:
    skip("the image's own accounts", "no Dockerfile next to this checkout")


# =====================================================================
# 3. The bridge seals before it launches, not after
# =====================================================================
print("\n--- the order of operations ---")

bridge_source = open(
    os.path.join(os.getcwd(), "app", "bridge", "mcp_browser.py"), encoding="utf-8"
).read()
seal_at = bridge_source.find("seal_environment()\n", bridge_source.find("async def page"))
launch_at = bridge_source.find("async_playwright().start()")
check("the environment is swept before anything of the browser's exists",
      0 < seal_at < launch_at, f"seal at {seal_at}, launch at {launch_at}")

# The sweep is applied to this process's own environment because Playwright
# reads os.environ when it starts its driver and takes no argument for it.
saved = dict(os.environ)
try:
    os.environ["AIOPS_SSHPASS_PROBE"] = "the-stored-password"
    os.environ["AIOPS_ASKPASS_PROBE"] = "/tmp/askpass"
    dropped = mb.seal_environment()
    check("sealing removes them from what any child would inherit",
          "AIOPS_SSHPASS_PROBE" not in os.environ and "AIOPS_ASKPASS_PROBE" not in os.environ,
          str(dropped))
    check("and says how many it took", "AIOPS_SSHPASS_PROBE" in dropped)
finally:
    for name in list(os.environ):
        if name not in saved:
            os.environ.pop(name, None)
    os.environ.update(saved)


# =====================================================================
# 4. What the browser user can actually reach. Measured, or skipped.
# =====================================================================
print("\n--- as the browser user ---")

HELPER = (settings.agent_runas or "").strip()
HAVE_HELPER = bool(HELPER) and POSIX and os.path.exists(HELPER)
if HAVE_HELPER:
    HAVE_HELPER = bool(stat.S_IMODE(os.stat(HELPER).st_mode) & stat.S_ISUID)


def as_browser(argv, env=None):
    """Run something as the browser user and hand back what happened."""
    return subprocess.run(
        [HELPER, "--as-browser", *argv], capture_output=True, timeout=30, env=env
    )


def as_agent(argv):
    return subprocess.run([HELPER, *argv], capture_output=True, timeout=30)


def text(result):
    return (result.stdout.decode("utf-8", "replace")
            + result.stderr.decode("utf-8", "replace")).strip()


if not HAVE_HELPER:
    skip("everything the browser user cannot reach",
         "no setuid helper here; these run inside the image")
else:
    identity = as_browser(["id"])
    who = text(identity)
    check("the helper starts a process as the browser user at all",
          identity.returncode == 0 and "uid=" in who, who)
    check("which is not the user running this test",
          f"uid={os.getuid()}(" not in who, who)
    check("and it is aiops-browser by name, so the uid is not a coincidence",
          "aiops-browser" in who, who)
    # The whole point. `node` is what makes a run's private keys readable.
    check("its groups do not include the agent's",
          "node" not in who and f"gid={os.getgid()}(" not in who, who)
    check("and it has exactly one group, its own",
          who.count("groups=") == 1 and who.split("groups=")[-1].count(",") == 0, who)

    # --- a real run's materials, not a fixture ------------------------
    KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "not-a-real-key-but-a-real-file\n"
           "-----END OPENSSH PRIVATE KEY-----")
    target = Target(
        id=1, name="Probe", slug="probe", hostname="127.0.0.1", username="probe",
        port=22, auth_type="key", private_key_enc=encrypt(KEY), password_enc=None,
        known_host_key=None, host_key_policy="accept-new", relay_node_id=None,
        description=None,
    )
    ctx = ssh_targets.prepare([target])
    assert ctx is not None
    try:
        key_path = os.path.join(ctx.root, "keys", "probe")
        learned = os.path.join(ctx.root, "known_hosts.learned")

        check("the run really did materialise a private key on disk",
              open(key_path).read().startswith("-----BEGIN"), key_path)

        denied = as_browser(["cat", key_path])
        check("DENIED: the browser user cannot read this run's private key",
              denied.returncode != 0 and "denied" in text(denied).lower(),
              text(denied) or f"exit {denied.returncode}")
        check("and the key's bytes are nowhere in what came back",
              "not-a-real-key" not in text(denied), text(denied))

        # The one file in a run directory that is group-*writable*, so ssh can
        # record a host key it just learned. Worth its own check: a mode that
        # loose is exactly where a mistake would show up first.
        denied = as_browser(["cat", learned])
        check("DENIED: nor the file the agent is allowed to write",
              denied.returncode != 0 and "denied" in text(denied).lower(),
              text(denied) or f"exit {denied.returncode}")

        denied = as_browser(["ls", os.path.join(ctx.root, "keys")])
        check("DENIED: it cannot even list the directory they are in",
              denied.returncode != 0, text(denied) or f"exit {denied.returncode}")

        # And the control: the same helper, the same file, the agent's uid.
        # Without this the three checks above would also pass against a key
        # that had simply not been written.
        allowed = as_agent(["cat", key_path])
        check("CONTROL: the agent can read it, which is why the run works",
              allowed.returncode == 0 and "not-a-real-key" in text(allowed),
              text(allowed)[:120] or f"exit {allowed.returncode}")
    finally:
        ctx.cleanup()

    # --- /app -------------------------------------------------------
    denied = as_browser(["ls", "/app"])
    check("DENIED: /app is unreadable to the browser user",
          denied.returncode != 0 and "denied" in text(denied).lower(),
          text(denied) or f"exit {denied.returncode}")

    # --- the environment, on the far side of the switch --------------
    poisoned = dict(os.environ)
    poisoned.update(RUN_ENVIRONMENT)
    seen = as_browser(["env"], env=poisoned)
    inherited = text(seen)
    check("DENIED: AIOPS_SSHPASS_* does not survive the switch",
          "AIOPS_SSHPASS_SONARR" not in inherited and "the-stored-password" not in inherited,
          inherited[:200])
    check("DENIED: nor does AIOPS_ASKPASS_*",
          "AIOPS_ASKPASS_BACKUP" not in inherited, inherited[:200])
    check("DENIED: nor the relay token or the loopback token",
          "relay-token-for-this-run" not in inherited
          and "loopback-token-for-this-run" not in inherited)
    check("DENIED: nor any AIOPS_ or POSTGRES_ variable at all",
          not re.search(r"^(AIOPS_|POSTGRES_|PG)", inherited, re.M), inherited[:400])
    check("the helper says where the browser may write instead",
          "HOME=/home/aiops-browser" in inherited and "TMPDIR=/home/aiops-browser" in inherited,
          inherited[:400])

    wrote = as_browser(["touch", "/home/aiops-browser/tmp/.probe"])
    check("and it can actually write there, or no browser would start",
          wrote.returncode == 0, text(wrote))
    as_browser(["rm", "-f", "/home/aiops-browser/tmp/.probe"])

    # --- screenshots ------------------------------------------------
    # Playwright's Python client writes them, in the bridge, at the agent's
    # uid — the browser hands back bytes and never opens a file. So the run's
    # screenshot directory is the agent's on both ends and the browser user
    # needs nothing there. Both halves of that are worth measuring.
    shot_dir = browsing.make_run_dir(9001)
    try:
        shot = os.path.join(shot_dir, "screenshot-001.png")
        with open(shot, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nnot-really-an-image")
        os.chmod(shot, 0o660)
        readable = as_agent(["cat", shot])
        check("the agent can read a screenshot in this run's directory",
              readable.returncode == 0 and b"PNG" in readable.stdout,
              text(readable)[:120] or f"exit {readable.returncode}")
        denied = as_browser(["cat", shot])
        check("DENIED: and the browser user cannot, because it never needs to",
              denied.returncode != 0, text(denied) or f"exit {denied.returncode}")
        check("the run directory's mode is the one browsing.py declares",
              stat.S_IMODE(os.stat(shot_dir).st_mode) == browsing.RUN_DIR_MODE,
              oct(stat.S_IMODE(os.stat(shot_dir).st_mode)))
    finally:
        browsing.grants.issue(9001, {}, None, "test", shot_dir)
        browsing.grants.revoke(9001)
    check("and it is gone when the run ends", not os.path.exists(shot_dir))


# =====================================================================
# 5. The real browser, really running as somebody else
# =====================================================================
print("\n--- the real browser ---")

SHIM = (settings.browser_runas or os.environ.get("AIOPS_BROWSER_RUNAS", "")).strip()


def process_table():
    """Every live process as (uid, cmdline). /proc is world-readable for this."""
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            uid = os.stat(f"/proc/{entry}").st_uid
            cmd = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if cmd.strip():
            rows.append((uid, cmd.strip()))
    return rows


async def live_checks():
    try:
        import playwright  # noqa: F401
    except ImportError:
        skip("the real browser", "Playwright is not installed in this environment")
        return
    if not HAVE_HELPER or not SHIM or not os.path.exists(SHIM):
        skip("the real browser", "no browser shim here; this runs inside the image")
        return

    mb.BROWSER_RUNAS = SHIM
    # The reach a run gets when one node covers one office network, and a
    # recorder for what the proxy decided about each request. `note` is what
    # the production code calls; overriding it here is how the decisions become
    # visible without an app to post them to.
    seen = []
    aiops = mb.Aiops()
    aiops.reach = {
        "routes": [{"node": "test-node", "host": "probe.invalid", "port": 8989}],
        "subnets": [], "systems": [],
    }
    aiops.note = lambda action, **fields: seen.append((action, fields))
    driver = mb.Browser(aiops)
    try:
        try:
            page = await driver.page()
        except mb.BrowserUnavailable as exc:
            skip("the real browser", f"Chromium would not start: {str(exc)[:200]}")
            return

        rows = process_table()
        mine = os.getuid()
        browser_rows = [(uid, cmd) for uid, cmd in rows if "headless_shell" in cmd]
        driver_rows = [(uid, cmd) for uid, cmd in rows if "cli.js" in cmd or "run-driver" in cmd]

        check("a real Chromium is running", bool(browser_rows), str(rows)[:400])
        check("and not one of its processes belongs to the user that asked for it",
              browser_rows and all(uid != mine for uid, _cmd in browser_rows),
              str([uid for uid, _ in browser_rows]))
        check("they all share one uid, which is not this one",
              len({uid for uid, _ in browser_rows}) == 1,
              str({uid for uid, _ in browser_rows}))
        check("Playwright's driver moved with them, so nothing between the "
              "renderer and the bridge is the agent's",
              driver_rows and all(uid != mine for uid, _cmd in driver_rows),
              str([uid for uid, _ in driver_rows]))

        browser_uid = browser_rows[0][0] if browser_rows else -1
        env_of = as_browser(["id", "-u"])
        check("and that uid is the one the helper reports",
              str(browser_uid) == text(env_of), f"{browser_uid} vs {text(env_of)}")

        # The proxy is a listener in *this* process, on loopback, and the
        # browser is now somebody else. Nothing about a TCP socket cares, but
        # "the gate is still in the path" is the one claim the uid split could
        # quietly have broken, so it is measured rather than reasoned about.
        # Both navigations fail on purpose; what is being read is where they
        # went. On a page of its own, because a navigation that failed is still
        # unwinding when goto() returns and destroys the execution context of
        # whatever is put on that page next.
        probe = await page.context.new_page()
        for url in ("http://probe.invalid:8989/", "http://127.0.0.1:8000/api/health"):
            try:
                await probe.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001 - the failure is the point
                pass
        await probe.close()
        check("a browser running as another user still reaches the proxy in this one",
              any(fields.get("host") == "probe.invalid" for _a, fields in seen), str(seen)[:300])
        check("and the routing decision is still made about what it asked for",
              any(fields.get("node") == "test-node" for _a, fields in seen), str(seen)[:300])
        check("BYPASS: loopback is refused to it exactly as before",
              any(action == "refused" and fields.get("host") == "127.0.0.1"
                  for action, fields in seen), str(seen)[:300])
        check("BYPASS: and nothing was opened to either of them",
              not any(action == "opened" for action, _f in seen), str(seen)[:300])

        # A page, and a screenshot of it, through the production code path.
        await page.set_content("<h1>Isolated</h1><input type=password value=hunter2>")
        body = await driver.read()
        check("the browser still works from the far side of the boundary",
              "Isolated" in body, body[:200])

        shot_dir = browsing.make_run_dir(9002)
        mb.SHOT_DIR = shot_dir
        # The operator's copy goes to AIOps over loopback, which is not running
        # here; stand in for it so the handover can be measured rather than
        # merely surviving a connection refusal.
        filed = []
        mb.TOKEN = "run-token"
        mb._post_bytes = lambda path, name, data, timeout=60: (
            filed.append((name, bytes(data))) or {"stored": True, "size": len(data)}
        )
        try:
            saved = await driver.screenshot()
            shot = os.path.join(shot_dir, "screenshot-001.png")
            info = os.stat(shot)
            check("it writes a real PNG into this run's directory",
                  open(shot, "rb").read(8) == b"\x89PNG\r\n\x1a\n", saved)
            check("written by the bridge, not by the browser — which is why the "
                  "browser needs no access to this directory",
                  info.st_uid == mine, f"owned by uid {info.st_uid}, we are {mine}")
            readable = as_agent(["cat", shot])
            check("and the agent can read it back to look at it",
                  readable.returncode == 0 and readable.stdout[:8] == b"\x89PNG\r\n\x1a\n",
                  text(readable)[:120] or f"exit {readable.returncode}")
            # And the copy that outlives the run: the same bytes, from the same
            # masked capture, handed over rather than read back off this
            # directory — which the agent can write to and the app must not
            # open a name in.
            check("the same bytes are handed to AIOps to keep with the session",
                  [n for n, _d in filed] == ["screenshot-001.png"]
                  and filed[0][1] == open(shot, "rb").read(),
                  str([(n, len(d)) for n, d in filed]))
        finally:
            browsing.grants.issue(9002, {}, None, "test", shot_dir)
            browsing.grants.revoke(9002)
    finally:
        await driver.close()

    after = [(uid, cmd) for uid, cmd in process_table() if "headless_shell" in cmd]
    check("and closing it leaves nothing of the browser's behind",
          not after, str(after)[:300])


asyncio.run(live_checks())


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
