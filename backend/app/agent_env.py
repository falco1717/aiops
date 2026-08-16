from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import signal
import stat
import subprocess
from collections.abc import Mapping, Sequence

from .config import settings

log = logging.getLogger("aiops.agent")

#: Names and prefixes an agent must never inherit. AIOPS_SECRET_KEY is the
#: worst of them — it decrypts every stored credential belonging to every user,
#: so an agent holding it makes the whole per-owner access model decorative.
#: AIOPS_DATABASE_URL reaches the ciphertext, AIOPS_JWT_SECRET forges any
#: user's session cookie, and AIOPS_ADMIN_PASSWORD is self-explanatory.
_BLOCKED_PREFIXES = ("AIOPS_", "POSTGRES_", "PG")
_BLOCKED_NAMES = frozenset(
    {
        "DATABASE_URL",
        "SECRET_KEY",
        "JWT_SECRET",
        "ADMIN_PASSWORD",
    }
)

#: Re-added deliberately after the sweep, because an agent legitimately needs
#: them and neither reveals anything: where its own binaries live, and where it
#: may write. Anything else AIOps wants an agent to have is passed explicitly
#: per run (the account's credential directory, the approval bridge's token).
_ALLOWED_AIOPS = frozenset({"AIOPS_WORKSPACE_ROOT"})

#: Signals the privileged helper will relay. Anything else is refused there too;
#: this list exists so a bad argument fails in Python rather than at the syscall.
_RELAYABLE = frozenset({signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGKILL", signal.SIGTERM)})

#: Group bits added to a tree the agent must be able to read (and write). The
#: agent's primary group is the app's own group, so sharing is a matter of group
#: bits alone — no chgrp, which matters because /workspaces is usually a bind
#: mount whose ownership comes from the host.
_DIR_SHARE = stat.S_IRGRP | stat.S_IXGRP
_FILE_SHARE = stat.S_IRGRP

#: A workspace can be a monorepo. The permission sweep stats every entry and
#: only writes where a bit is actually missing, so the steady-state cost is a
#: walk — but an unbounded walk at startup is still a way to hang a boot.
MAX_SHARE_ENTRIES = 200_000


def agent_environ() -> dict[str, str]:
    """The environment an agent subprocess starts from.

    An agent runs arbitrary commands by design, so anything in its environment
    is something it can read and act on. The control plane's own secrets were
    reaching it simply because the app inherited them and passed its whole
    environment down.

    Stripping them is necessary but was never sufficient on its own: an agent
    sharing a UID with the app can read the app's own environment back out of
    /proc, which is why every agent process is also started under a separate
    user (see `spawn`). The two halves are here together because neither works
    without the other.
    """
    safe: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _ALLOWED_AIOPS:
            safe[name] = value
            continue
        if upper in _BLOCKED_NAMES:
            continue
        if any(upper.startswith(prefix) for prefix in _BLOCKED_PREFIXES):
            continue
        safe[name] = value
    return safe


# -- running as somebody else ------------------------------------------


def isolation_enabled() -> bool:
    """True when agent processes are started under their own user."""
    return bool((settings.agent_runas or "").strip())


def runas_argv(argv: Sequence[str]) -> list[str]:
    """Prefix a command with the privilege-dropping helper.

    `asyncio.create_subprocess_exec(user=...)` would be the obvious way to do
    this, but it needs the *parent* to be privileged and the app deliberately
    runs as an unprivileged user. The helper is a small setuid-root binary that
    drops to the agent's uid and execs — so the app never holds root, and the
    thing that does holds it for a few syscalls.
    """
    if not isolation_enabled():
        return list(argv)
    return [settings.agent_runas.strip(), *argv]


async def spawn(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    **kwargs,
) -> asyncio.subprocess.Process:
    """Start a process on the agent's side of the boundary.

    Every subprocess this application creates is an agent's: the two CLIs, the
    sign-in flows that write into their credential directories, and `git` read
    against a working tree an agent has been editing. They are all started here
    so that the environment sweep and the user switch cannot be forgotten at a
    call site — the isolation test asserts there is no other spawn in the app.

    `env` is merged over the swept environment, never used instead of it.
    """
    full: dict[str, str] = {**agent_environ(), **(env or {})}
    command = runas_argv(argv)
    if isolation_enabled():
        # The helper always exists, so a missing CLI would otherwise surface as
        # exit code 127 with the helper's own error text instead of the
        # FileNotFoundError every caller here already handles.
        if shutil.which(argv[0], path=full.get("PATH")) is None:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", argv[0])
        # /app is not readable by the agent user, and a process whose cwd it
        # cannot resolve gets confusing failures out of anything that calls
        # getcwd(). Callers that care pass their own cwd; this is for the rest.
        kwargs.setdefault("cwd", settings.workspace_root)
    if os.name == "posix":
        # One process group per agent, so a runaway that spawned children can be
        # stopped as a unit. The helper below signals by group for the same
        # reason.
        kwargs.setdefault("start_new_session", True)
    return await asyncio.create_subprocess_exec(*command, env=full, **kwargs)


def signal_agent(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal an agent process and everything it started.

    Under isolation the app cannot signal the agent directly — different uid,
    EPERM — so the same helper does it, refusing any target that is not an
    agent process. Without isolation this is the plain killpg it always was.
    """
    if proc.returncode is None:
        _signal_pid(proc, sig)


def _signal_pid(proc: asyncio.subprocess.Process, sig: int) -> None:
    if isolation_enabled() and os.name == "posix" and sig in _RELAYABLE:
        pgid = _pgid(proc.pid)
        try:
            result = subprocess.run(
                [
                    settings.agent_runas.strip(),
                    "--kill-group" if pgid is not None else "--kill",
                    str(int(sig)),
                    str(pgid if pgid is not None else proc.pid),
                ],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not signal agent process %s: %s", proc.pid, exc)
            return
        if result.returncode != 0:
            # Nothing matched is the ordinary case for a process that has
            # already exited, so this is a debug line, not a warning.
            log.debug(
                "helper signalled nothing for pid %s: %s",
                proc.pid,
                result.stderr.decode("utf-8", "replace").strip(),
            )
        return

    if hasattr(os, "killpg"):
        pgid = _pgid(proc.pid)
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        if sig == getattr(signal, "SIGKILL", signal.SIGTERM) and hasattr(signal, "SIGKILL"):
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass


def _pgid(pid: int) -> int | None:
    if not hasattr(os, "getpgid"):
        return None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return None


def kill_agent(proc: asyncio.subprocess.Process) -> None:
    """The `proc.kill()` every timeout path used to call."""
    signal_agent(proc, getattr(signal, "SIGKILL", signal.SIGTERM))


def probe_identity() -> str:
    """Who an agent process actually runs as, asked of the helper itself.

    Logged at startup: a helper that is present but not setuid fails every run,
    and finding that out from a failed turn at 3am is worse than a line in the
    boot log.
    """
    if not isolation_enabled():
        return "disabled (agents share the application's user)"
    try:
        result = subprocess.run(
            [settings.agent_runas.strip(), "id"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"BROKEN: {exc}"
    if result.returncode != 0:
        return f"BROKEN: {result.stderr.decode('utf-8', 'replace').strip() or result.returncode}"
    return result.stdout.decode("utf-8", "replace").strip()


def browser_isolation_enabled() -> bool:
    """True when the browser runs as its own user rather than as the agent."""
    return isolation_enabled() and bool((settings.browser_runas or "").strip())


def probe_browser_identity() -> str:
    """Who Chromium actually runs as, asked of the helper in the same way.

    Worth its own line at boot next to the agent's. The browser is the only
    thing in this container that executes content nobody vetted, and "which
    user is that" is the answer the whole isolation rests on — a shim that is
    present but pointed at nothing, or a helper built without the browser mode,
    would otherwise be discovered from a failed turn.
    """
    if not isolation_enabled():
        return "disabled (the browser shares the application's user)"
    if not browser_isolation_enabled():
        return "disabled (the browser runs as the agent user)"
    try:
        result = subprocess.run(
            [settings.agent_runas.strip(), "--as-browser", "id"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"BROKEN: {exc}"
    if result.returncode != 0:
        return f"BROKEN: {result.stderr.decode('utf-8', 'replace').strip() or result.returncode}"
    return result.stdout.decode("utf-8", "replace").strip()


# -- what the agent may reach ------------------------------------------


def helper_script(name: str, fallback: str) -> str:
    """Path to a standalone helper the *agent* has to be able to execute.

    Two scripts in this package are run from the agent's side of the boundary:
    the MCP approval bridge, which the CLI spawns, and the relay ProxyCommand,
    which ssh spawns. The application's own source is not readable by the agent
    user, so the image installs a copy of each somewhere neutral and this
    prefers it. Outside the image — tests, a dev checkout — the package's own
    copy is used, which is the same file.
    """
    installed = os.path.join(settings.agent_helper_dir, name)
    if settings.agent_helper_dir and os.path.isfile(installed):
        return installed
    return fallback


def grant_agent_access(
    path: str, *, writable: bool, recursive: bool = True
) -> tuple[int, int]:
    """Make a tree reachable by the agent user, adding bits and never removing.

    Everything shared with an agent — workspaces, the credential directories,
    attachments — is owned by the app and shares its group with the agent, so
    access is decided by the group bits. They cannot simply be set at build
    time: these are volumes and bind mounts, and their contents were written
    before this boundary existed (a credential file at 0600 is the normal case,
    not the exception).

    Returns (entries examined, entries changed).
    """
    if os.name != "posix" or not path:
        return (0, 0)
    seen = changed = 0
    if _add_bits(path, os.path.isdir(path), writable):
        changed += 1
    seen += 1
    if not recursive or not os.path.isdir(path):
        return (seen, changed)

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            seen += 1
            if _add_bits(os.path.join(root, name), True, writable):
                changed += 1
        for name in files:
            seen += 1
            if _add_bits(os.path.join(root, name), False, writable):
                changed += 1
        if seen > MAX_SHARE_ENTRIES:
            log.warning(
                "stopped sharing %s with the agent user after %d entries; "
                "anything deeper keeps whatever permissions it already had",
                path,
                seen,
            )
            return (seen, changed)
    return (seen, changed)


def _add_bits(path: str, is_dir: bool, writable: bool) -> bool:
    wanted = _DIR_SHARE if is_dir else _FILE_SHARE
    if writable:
        wanted |= stat.S_IWGRP
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        # The target is walked on its own if it is inside the tree, and a
        # symlink out of it is not ours to widen.
        return False
    mode = stat.S_IMODE(info.st_mode)
    if mode & wanted == wanted:
        return False
    try:
        os.chmod(path, mode | wanted)
    except OSError as exc:
        # Not ours to change — an operator's file in a bind-mounted workspace,
        # most often. Worth one line, not a failed boot.
        log.debug("could not share %s with the agent user: %s", path, exc)
        return False
    return True


def share_startup_paths() -> None:
    """Bring every long-lived shared path up to the boundary's expectations."""
    if not isolation_enabled():
        return
    home = os.path.expanduser("~")
    targets: list[tuple[str, bool, bool]] = [
        (settings.workspace_root, True, True),
        (settings.accounts_root, True, True),
        (settings.attachments_root, False, True),
        # $HOME itself, so a CLI with no named account can still write the
        # dotfiles it expects; its two default credential directories are
        # shared in full.
        (home, True, False),
        (os.path.join(home, ".claude"), True, True),
        (os.path.join(home, ".codex"), True, True),
    ]
    for path, writable, recursive in targets:
        if not os.path.isdir(path):
            continue
        seen, changed = grant_agent_access(path, writable=writable, recursive=recursive)
        if changed:
            log.info("shared %s with the agent user (%d of %d entries)", path, changed, seen)
    if not os.access(settings.accounts_root, os.W_OK):
        log.error(
            "%s is not writable by this process, so named provider accounts cannot be "
            "created. It is a Docker volume seeded before the image created the "
            "directory; recreate it (docker volume rm <stack>_agent-accounts) or chown "
            "it to the application user.",
            settings.accounts_root,
        )
