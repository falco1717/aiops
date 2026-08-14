"""The boundary between an agent and the control plane.

This suite exists because of an incident, not a hypothesis. An agent running a
perfectly ordinary turn read AIOPS_DATABASE_URL out of its own environment,
connected to Postgres, dumped `targets.private_key_enc`, read `app/crypto.py` to
learn how the key is derived, derived it from AIOPS_SECRET_KEY, decrypted
another user's SSH private key, and used it.

The first fix stripped those variables from the agent's environment. That alone
was never enough: the agent ran as the same uid as the app, so `cat
/proc/<app-pid>/environ` handed back everything that had just been stripped.
The fix this covers is a second user — every agent process is started through a
setuid helper that drops to it — plus the file permissions that has to come
with.

Everything here is assertable without Docker. The parts that are not (that
/proc really is unreadable, that a real `claude` turn and a real `ssh` to a
stored system still work under the switch) are verified against a built image on
the server; there is no way to fake those and no point pretending otherwise.
"""
import ast
import os
import stat
import subprocess
import sys

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-isolation.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
# Named exactly as the incident had them, so this fails if the sweep is ever
# narrowed to a hand-written list of the ones we happened to think of.
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password-in-the-environment")
os.environ.setdefault("PGPASSWORD", "another-postgres-password")
os.environ.setdefault("DATABASE_URL", "postgresql://someone:secret@db:5432/aiops")

from app import agent_env  # noqa: E402
from app.config import settings  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.models import Target  # noqa: E402
from app import ssh_targets  # noqa: E402

POSIX = os.name == "posix"
APP_DIR = os.path.join(os.getcwd(), "app")
ROOT = os.path.dirname(os.getcwd())

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- 1. the environment an agent starts from --------------------------
env = agent_env.agent_environ()

for name in (
    "AIOPS_SECRET_KEY",
    "AIOPS_DATABASE_URL",
    "AIOPS_JWT_SECRET",
    "AIOPS_ADMIN_PASSWORD",
    "POSTGRES_PASSWORD",
    "PGPASSWORD",
    "DATABASE_URL",
):
    check(f"{name} is withheld from an agent", name not in env)

# The values, not just the names: a variable copied under another name is the
# same disclosure.
leaked = [
    name
    for name, value in env.items()
    if value
    and value
    in (
        os.environ["AIOPS_SECRET_KEY"],
        os.environ["AIOPS_JWT_SECRET"],
        os.environ["POSTGRES_PASSWORD"],
        os.environ["DATABASE_URL"],
    )
]
check("no secret's value survives under any other name", not leaked, ", ".join(leaked))

check("the workspace root does survive, because an agent needs it",
      env.get("AIOPS_WORKSPACE_ROOT") == os.environ.get("AIOPS_WORKSPACE_ROOT", env.get("AIOPS_WORKSPACE_ROOT")))
check("PATH survives, or nothing would be runnable at all", bool(env.get("PATH")))


# --- 2. one chokepoint, and no way around it --------------------------
SPAWNERS = {"create_subprocess_exec", "create_subprocess_shell", "Popen", "run", "call",
            "check_call", "check_output", "system", "spawnv", "spawnvpe", "posix_spawn"}
MODULES = {"asyncio", "subprocess", "os"}

spawn_sites = []
environ_as_env = []

for dirpath, dirnames, filenames in os.walk(APP_DIR):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for filename in sorted(filenames):
        if not filename.endswith(".py"):
            continue
        path = os.path.join(dirpath, filename)
        rel = os.path.relpath(path, APP_DIR).replace("\\", "/")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in SPAWNERS:
                base = func.value
                # asyncio.create_subprocess_exec, subprocess.run, os.system…
                root = base
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in MODULES:
                    spawn_sites.append((rel, node.lineno, func.attr))
            for kw in node.keywords:
                if kw.arg != "env":
                    continue
                value = kw.value
                if isinstance(value, ast.Attribute) and value.attr == "environ":
                    environ_as_env.append((rel, node.lineno))
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                        and value.func.attr in ("copy", "dict") \
                        and isinstance(value.func.value, ast.Attribute) \
                        and value.func.value.attr == "environ":
                    environ_as_env.append((rel, node.lineno))

outside = sorted({site for site in spawn_sites if site[0] != "agent_env.py"})
check(
    "no module starts a process except the boundary itself",
    not outside,
    "; ".join(f"{f}:{line} {name}" for f, line, name in outside),
)
check(
    "the boundary does start one",
    any(site[0] == "agent_env.py" for site in spawn_sites),
)
check(
    "no module hands its own environment to a subprocess",
    not environ_as_env,
    "; ".join(f"{f}:{line}" for f, line in environ_as_env),
)

# The chokepoint has to actually apply both halves: the sweep and the switch.
with open(os.path.join(APP_DIR, "agent_env.py"), encoding="utf-8") as fh:
    boundary = ast.parse(fh.read())
spawn_fn = next(
    (n for n in boundary.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "spawn"), None
)
check("the boundary exposes an async spawn()", spawn_fn is not None)
if spawn_fn is not None:
    body = ast.dump(spawn_fn)
    check("spawn() builds its environment from agent_environ()", "agent_environ" in body)
    check("spawn() runs the command through runas_argv()", "runas_argv" in body)


# --- 3. the user switch ----------------------------------------------
original = settings.agent_runas
try:
    settings.agent_runas = ""
    check("with no helper configured, argv is untouched",
          agent_env.runas_argv(["claude", "-p", "hi"]) == ["claude", "-p", "hi"])
    check("and isolation reports itself as off", not agent_env.isolation_enabled())

    settings.agent_runas = "/usr/local/bin/aiops-runas"
    check("with a helper configured, every command goes through it",
          agent_env.runas_argv(["claude", "-p", "hi"])
          == ["/usr/local/bin/aiops-runas", "claude", "-p", "hi"])
    check("and isolation reports itself as on", agent_env.isolation_enabled())
finally:
    settings.agent_runas = original

# End to end through the real chokepoint, with a process that reports what it
# was actually given. No user switch involved — that needs the image — but this
# is the code path a run takes.
import asyncio  # noqa: E402

probe = "import json,os;print(json.dumps(dict(os.environ)))"


async def _probe():
    proc = await agent_env.spawn(
        [sys.executable, "-c", probe],
        env={"AIOPS_PROBE_MARKER": "visible"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out


import json  # noqa: E402

child = json.loads(asyncio.run(_probe()).decode())
check("a real child gets no AIOPS_SECRET_KEY", "AIOPS_SECRET_KEY" not in child)
check("a real child gets no AIOPS_DATABASE_URL", "AIOPS_DATABASE_URL" not in child)
check("a real child gets no POSTGRES_PASSWORD", "POSTGRES_PASSWORD" not in child)
check("a real child does get what the caller passed it",
      child.get("AIOPS_PROBE_MARKER") == "visible")


# --- 4. the per-run SSH directory ------------------------------------
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----"
target = Target(
    id=1,
    name="Probe",
    slug="probe",
    hostname="127.0.0.1",
    username="probe",
    port=22,
    auth_type="key",
    private_key_enc=encrypt(KEY),
    password_enc=None,
    known_host_key=None,
    host_key_policy="accept-new",
    relay_node_id=None,
    description=None,
)

ctx = ssh_targets.prepare([target])
check("a run with a stored system gets a directory", ctx is not None)
assert ctx is not None
try:
    key_path = os.path.join(ctx.root, "keys", "probe")
    config_path = os.path.join(ctx.root, "config")
    shim_path = os.path.join(ctx.root, "bin", "ssh")

    check("the private key is written where the run's ssh config points",
          os.path.exists(key_path) and open(key_path).read().startswith(KEY.splitlines()[0]))

    # A strict host must be verified against the pinned file only. If the
    # agent-writable file were offered to it as well, the agent could add an
    # entry for a host it was about to dial and walk straight past
    # StrictHostKeyChecking.
    config_text = open(config_path).read()
    check("an accept-new host may record what it learns",
          "known_hosts.learned" in config_text, config_text)

    strict_target = Target(
        id=902, name="Strict Probe", slug="strict-probe", hostname="127.0.0.1",
        username="probe", port=22, auth_type="key", private_key_enc=encrypt(KEY),
        password_enc=None, known_host_key=None, host_key_policy="strict",
        relay_node_id=None, description=None,
    )
    strict_ctx = ssh_targets.prepare([strict_target])
    assert strict_ctx is not None
    try:
        strict_text = open(os.path.join(strict_ctx.root, "config")).read()
        check("a strict host is never offered the agent-writable file",
              "known_hosts.learned" not in strict_text, strict_text)
        check("a strict host still gets the pinned file",
              "UserKnownHostsFile" in strict_text and "known_hosts" in strict_text)
    finally:
        strict_ctx.cleanup()

    check("the run directory's mode is the one this module declares",
          ssh_targets.RUN_DIR_MODE == 0o750)
    check("secrets in it are readable by the agent's group and nobody else",
          ssh_targets.RUN_FILE_MODE == 0o640)
    check("an agent cannot edit its own config or keys",
          not (ssh_targets.RUN_FILE_MODE | ssh_targets.RUN_DIR_MODE) & stat.S_IWGRP)
    # The single exception, and it is deliberate: ssh records a host key it has
    # just learned, and the agent is what runs ssh. It gets its own file for
    # that so the operator's pinned keys stay out of its reach.
    check("only the learned-hosts file is group-writable",
          ssh_targets.RUN_SHARED_MODE & stat.S_IWGRP
          and not ssh_targets.RUN_SHARED_MODE & stat.S_IRWXO,
          oct(ssh_targets.RUN_SHARED_MODE))
    check("nothing in it is readable by anyone else at all",
          not (ssh_targets.RUN_FILE_MODE | ssh_targets.RUN_DIR_MODE | ssh_targets.RUN_EXEC_MODE)
          & (stat.S_IRWXO))

    if POSIX:
        for label, path, expected in (
            ("run directory", ctx.root, ssh_targets.RUN_DIR_MODE),
            ("keys directory", os.path.join(ctx.root, "keys"), ssh_targets.RUN_DIR_MODE),
            ("bin directory", os.path.join(ctx.root, "bin"), ssh_targets.RUN_DIR_MODE),
            ("private key", key_path, ssh_targets.RUN_FILE_MODE),
            ("ssh config", config_path, ssh_targets.RUN_FILE_MODE),
            ("known_hosts", os.path.join(ctx.root, "known_hosts"), ssh_targets.RUN_FILE_MODE),
            ("learned known_hosts", os.path.join(ctx.root, "known_hosts.learned"),
             ssh_targets.RUN_SHARED_MODE),
            ("ssh shim", shim_path, ssh_targets.RUN_EXEC_MODE),
        ):
            actual = stat.S_IMODE(os.stat(path).st_mode)
            check(f"the {label} is really {oct(expected)} on disk", actual == expected,
                  oct(actual))
    else:
        print("[skip] file modes are not meaningful on this platform")
finally:
    ctx.cleanup()
check("and the whole directory is gone when the run ends", not os.path.exists(ctx.root))


# --- 5. sharing a tree with the agent user ----------------------------
if POSIX:
    import tempfile

    tree = tempfile.mkdtemp(prefix="aiops-share-test-")
    inner = os.path.join(tree, "inner")
    os.makedirs(inner)
    leaf = os.path.join(inner, "file")
    with open(leaf, "w") as fh:
        fh.write("x")
    os.chmod(tree, 0o700)
    os.chmod(inner, 0o700)
    os.chmod(leaf, 0o600)

    seen, changed = agent_env.grant_agent_access(tree, writable=True)
    check("sharing a tree reaches every entry in it", seen == 3, f"{seen} entries")
    check("and changes the ones that needed it", changed == 3, f"{changed} changed")
    check("a shared directory becomes group rwx",
          stat.S_IMODE(os.stat(inner).st_mode) == 0o770, oct(stat.S_IMODE(os.stat(inner).st_mode)))
    check("a shared file becomes group rw",
          stat.S_IMODE(os.stat(leaf).st_mode) == 0o660, oct(stat.S_IMODE(os.stat(leaf).st_mode)))
    check("nothing is opened up to other users",
          not stat.S_IMODE(os.stat(leaf).st_mode) & stat.S_IRWXO)

    again = agent_env.grant_agent_access(tree, writable=True)
    check("a second pass changes nothing, so booting twice is not a slow no-op",
          again[1] == 0, f"{again[1]} changed")

    os.chmod(leaf, 0o600)
    seen, changed = agent_env.grant_agent_access(tree, writable=False, recursive=False)
    check("a read-only share does not hand out write", changed == 0 or
          not stat.S_IMODE(os.stat(leaf).st_mode) & stat.S_IWGRP)

    import shutil

    shutil.rmtree(tree, ignore_errors=True)
else:
    print("[skip] POSIX permissions are not meaningful on this platform")


# --- 6. the helpers an agent has to be able to execute ----------------
fallback = os.path.join(APP_DIR, "bridge", "mcp_approver.py")
original_dir = settings.agent_helper_dir
try:
    settings.agent_helper_dir = os.path.join(ROOT, "does-not-exist")
    check("with nothing installed, the package's own copy is used",
          agent_env.helper_script("mcp_approver.py", fallback) == fallback)
finally:
    settings.agent_helper_dir = original_dir

from app.providers.claude import BRIDGE_SCRIPT  # noqa: E402

check("the approval bridge resolves to a file that exists", os.path.isfile(str(BRIDGE_SCRIPT)))
check("the relay ProxyCommand resolves to a file that exists",
      os.path.isfile(ssh_targets.PROXY_HELPER))


# --- 7. the image is wired to match ------------------------------------
# The code above is only half of it. A correct boundary in an image that never
# creates the second user is a boundary that does not exist.
with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
    dockerfile = fh.read()

for label, needle in (
    ("the image creates the agent user", "useradd --uid ${AGENT_UID} --gid node"),
    ("the helper is built from source in the image", "aiops-runas.c"),
    ("the helper is setuid and group-restricted", "chmod 4750 /usr/local/bin/aiops-runas"),
    ("agents are told to use it", "AIOPS_AGENT_RUNAS=/usr/local/bin/aiops-runas"),
    ("the application source is closed to everyone but the app",
     "chmod -R u=rwX,go= /app"),
    ("the approval bridge is installed outside /app", "/opt/aiops-agent/mcp_approver.py"),
    ("the relay ProxyCommand is installed outside /app", "/opt/aiops-agent/relay_connect.py"),
    ("the app itself still runs as node", "USER node"),
):
    check(label, needle in dockerfile)

check("the helper source is in the repository",
      os.path.isfile(os.path.join(ROOT, "deploy", "runas", "aiops-runas.c")))

with open(os.path.join(ROOT, "deploy", "runas", "aiops-runas.c"), encoding="utf-8") as fh:
    helper_source = fh.read()
for label, needle in (
    ("the helper drops the group list before the uid", "setgroups"),
    ("the helper drops all three uids, not just the effective one", "setresuid"),
    ("the helper proves it cannot get back to root", "setuid(0) == 0"),
    ("the helper serves only the application's uid", "getuid() != (uid_t)AIOPS_APP_UID"),
    ("the helper will only signal agent processes", "owned_by_agent"),
):
    check(label, needle in helper_source)


# --- 8. what this actually looks like in the image --------------------
# Skipped outside the container, where there is no helper to ask.
if agent_env.isolation_enabled() and POSIX and os.path.exists(settings.agent_runas):
    identity = agent_env.probe_identity()
    check("the helper reports a working identity", "uid=" in identity, identity)
    check("which is not the application's own user",
          f"uid={os.getuid()}(" not in identity, identity)
    proc = subprocess.run(
        [settings.agent_runas, "sh", "-c", "cat /proc/1/environ"],
        capture_output=True,
    )
    check("and cannot read the init process's environment",
          proc.returncode != 0 and b"AIOPS_SECRET_KEY" not in proc.stdout,
          proc.stderr.decode("utf-8", "replace").strip()[:120])
else:
    print("[skip] no isolation helper here; the image is verified on the server")


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
