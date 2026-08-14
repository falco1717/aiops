from __future__ import annotations

import os

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


def agent_environ() -> dict[str, str]:
    """The environment an agent subprocess starts from.

    An agent runs arbitrary commands by design, so anything in its environment
    is something it can read and act on. The control plane's own secrets were
    reaching it simply because the app inherited them and passed its whole
    environment down.

    This is containment, not isolation. The agent still shares a UID and a
    filesystem with the app, so a determined one can read the application
    source, and on Linux it can read the app process's own environment through
    /proc. Closing that requires running agents under a separate user or in a
    separate container; until then this removes the casual path, not the
    determined one.
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
