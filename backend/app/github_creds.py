from __future__ import annotations

import logging
import os
import shlex
import shutil
import tempfile

from .crypto import SecretUnavailable, decrypt
from .models import GithubAccount

log = logging.getLogger("aiops.github")

#: Same shape as ssh_targets.RUN_*_MODE, and for the same reason: the app owns
#: these files, the agent reads (and, for the directory, executes into) them
#: through the group bit, and nothing here is group-writable.
RUN_DIR_MODE = 0o750
RUN_EXEC_MODE = 0o750

#: The only host this run's credential helper will ever answer for. Bound into
#: the generated gitconfig as a URL-scoped `credential.<url>.helper` section —
#: git itself only ever invokes a URL-scoped helper for a request matching that
#: URL — so a workspace whose remote points somewhere else never has this
#: token handed to it, and a repo added *inside* the workspace pointing at
#: another host gets no credential either.
GITHUB_HOST = "https://github.com"


class GitCredContext:
    """Per-run (or per-operation) git credential materials for one GitHub account.

    Same shape as `ssh_targets.SshContext`: a private directory holding
    everything, an environment to merge into the subprocess, and a `cleanup()`
    the caller runs in its `finally`. Nothing here is written into the
    workspace itself — the directory is a throwaway temp dir elsewhere on
    disk — so a `cat` or `git log` run *inside* the workspace can never turn it
    up, and neither can `env`: only a path to this directory is exposed, via
    `GIT_CONFIG_GLOBAL`.

    Like `ssh_targets`' askpass helper for a key passphrase, the credential
    helper script this writes has the token baked into its own text, at the
    same file mode ssh's askpass helper uses (0750: the app owns it, the agent
    group can read and execute it, nothing else can). That is a deliberate,
    pre-existing trade-off in this codebase rather than a new one: git has to
    invoke this helper as the agent user, which means the agent user must be
    able to *read* the script to run it at all — a script that can be executed
    but not read is not something a shell interpreter can do anything with.
    So, exactly as an agent could already `cat` an ssh askpass helper to read
    out a stored key's passphrase, an agent that goes looking for this
    specific file (found by reading `GIT_CONFIG_GLOBAL` out of its own
    environment) could read the token out of it too. What this *does* buy over
    putting the token directly in an environment variable: `env`, `git log`,
    and reading any file that is actually part of the checkout never recover
    it, and the ordinary path — running `git push`/`git pull`/`git fetch` —
    never prints it anywhere.
    """

    def __init__(self, root: str, env: dict[str, str]) -> None:
        self.root = root
        self.env = env

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def prepare(account: GithubAccount, *, prefix: str = "aiops-git-") -> GitCredContext | None:
    """Materialise one GitHub account's token as a git credential helper.

    None when the account has no usable token, so a caller can treat that
    exactly like `ssh_targets.prepare` treats an unreadable stored key: log it
    and continue without credentials rather than take the whole run down.
    """
    try:
        token = decrypt(account.token_enc)
    except SecretUnavailable as exc:
        log.warning("github account %s skipped: %s", account.id, exc)
        return None
    if not token:
        log.warning("github account %s has no stored token", account.id)
        return None

    root = tempfile.mkdtemp(prefix=prefix)
    os.chmod(root, RUN_DIR_MODE)

    helper_path = os.path.join(root, "credential-helper")
    _write_helper(helper_path, token)

    gitconfig_path = os.path.join(root, "gitconfig")
    _write_gitconfig(gitconfig_path, helper_path)

    env = {
        # Entirely replaces where git looks for its "global" config for any
        # process given this environment — it does not merge with the
        # invoking user's own ~/.gitconfig. That is exactly what is wanted
        # here: this run gets github.com credentials and nothing else changes
        # about how git behaves for it.
        "GIT_CONFIG_GLOBAL": gitconfig_path,
        # Never fall back to an interactive prompt. An agent's git command has
        # no terminal a human is watching, so a prompt would just hang the
        # turn until its timeout — worse than a clean authentication failure.
        "GIT_TERMINAL_PROMPT": "0",
    }
    return GitCredContext(root, env)


def _write_helper(path: str, token: str) -> None:
    """A git credential helper that answers a `get` request with this token.

    Speaks the plain "get" verb of git's credential helper protocol
    (https://git-scm.com/docs/git-credential) and nothing else: `store` and
    `erase` requests are read and ignored, so nothing this run does can make
    git cache the token anywhere more persistent than this directory — no
    `~/.git-credentials`, no OS keychain entry.

    The token is written into the script's own text, the same way
    `ssh_targets._write_askpass` bakes in a key passphrase, and for the same
    reason: git execs this file directly, so whatever it prints has to already
    be inside it — there is nothing to look up elsewhere at the moment git
    asks. `shlex.quote` keeps a token containing a quote or backslash from
    escaping the string it is printed from.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, RUN_EXEC_MODE)
    with os.fdopen(handle, "w", newline="\n") as fh:
        fh.write(
            "#!/bin/sh\n"
            "# Generated by AIOps for one run. Deleted afterwards.\n"
            "# Answers a git credential 'get' request for github.com only;\n"
            "# 'store' and 'erase' are accepted and ignored so nothing here\n"
            "# is ever cached anywhere more persistent than this directory.\n"
            'case "$1" in\n'
            "  get)\n"
            f"    printf 'username=x-access-token\\npassword=%s\\n' {shlex.quote(token)}\n"
            "    ;;\n"
            "esac\n"
        )
    os.chmod(path, RUN_EXEC_MODE)


def _write_gitconfig(path: str, helper_path: str) -> None:
    """The config `GIT_CONFIG_GLOBAL` points at.

    `[credential] helper =` first, with no value, clears any helper chain a
    *system*-level config (`/etc/gitconfig`, which `GIT_CONFIG_GLOBAL` does not
    override) might otherwise contribute — the standard git idiom for
    resetting the helper list before adding one back. The specific helper is
    then registered only for `https://github.com`, so it is never consulted
    for any other remote a workspace — or a repo an agent adds inside one —
    might point at.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(handle, "w", newline="\n") as fh:
        fh.write(
            "# Generated by AIOps for one run. Do not edit — it is deleted afterwards.\n"
            "[credential]\n"
            "\thelper =\n"
            f'[credential "{GITHUB_HOST}"]\n'
            f"\thelper = {_quote_config(helper_path)}\n"
        )
    os.chmod(path, 0o640)


def _quote_config(value: str) -> str:
    """Escape a value for a git config file's double-quoted form.

    Only backslash and double-quote are special inside a quoted config value;
    a path is most likely to contain neither, but a temp directory name is not
    something to trust blindly.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
