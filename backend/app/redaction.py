"""Strip secrets out of text that is about to be shown to an agent again.

A transcript is written by processes AIOps does not control, so it can contain
things AIOps went to some trouble never to hand out. That already happened here
once: an agent ran a command that dumped its environment, and `AIOPS_SECRET_KEY`
— the key that decrypts every stored credential belonging to every user — landed
in a tool result and stayed in the database.

Reading such a transcript back and *replaying* it into another agent's prompt
turns one leak into a spreading one, so anything assembled out of stored events
goes through here first. Two independent passes, because either alone fails in a
way the other catches:

* the pattern pass masks `NAME=value` and `NAME: value` for names that mean
  "secret", which catches secrets belonging to somebody else entirely — a key
  the agent read out of a customer's `.env` is not ours and is not in our config;
* the literal pass masks the values this process actually holds, whatever
  syntax they were printed in. `AIOPS_SECRET_KEY` printed by `env` matches the
  first pass; the same value echoed bare in the middle of a sentence, or
  interpolated into a URL, only matches this one.

Nothing here is a substitute for keeping secrets out of agents' hands in the
first place (see agent_env.py). It is the last stop before something already
leaked is leaked further.
"""

from __future__ import annotations

import re

from .config import settings

MASK = "[redacted]"

#: Words that make a name a secret. Matched inside the name, so AIOPS_SECRET_KEY,
#: GITHUB_TOKEN, db_password and "Api-Key" are all caught by one list.
_SECRET_WORDS = (
    "secret",
    "password",
    "passwd",
    "passphrase",
    "token",
    "api[_-]?key",
    "access[_-]?key",
    "private[_-]?key",
    "credential",
    "auth",
    "jwt",
    "session[_-]?key",
    "bearer",
)

_NAME = rf"[A-Za-z0-9_.\-]*(?:{'|'.join(_SECRET_WORDS)})[A-Za-z0-9_.\-]*"

#: `NAME=value`, as `env`, `printenv`, `set` and a shell assignment all print it.
#: The value runs to end of line: a secret with a space in it is still a secret,
#: and over-masking the rest of a line costs nothing here.
_ASSIGNMENT = re.compile(rf"(?im)^(\s*(?:export\s+|set\s+)?{_NAME}\s*=\s*)(\S.*)$")

#: The same idea in YAML, JSON and log-line shapes: `name: value`, `"name": "v"`.
_COLON = re.compile(rf"(?im)^(\s*[\"']?{_NAME}[\"']?\s*:\s*)(\S.*)$")

#: Inline, mid-sentence spellings the two line-anchored patterns above miss.
_INLINE = re.compile(rf"(?i)\b({_NAME})(\s*[=:]\s*)([\"']?)([A-Za-z0-9_\-./+=]{{8,}})\3")

#: An agent narrating a secret in prose — "the token is abc…", "password abc…" —
#: with no punctuation to key on. Deliberately narrower than the patterns above:
#: it needs a long, unbroken, credential-shaped run of characters right after the
#: word, because at ordinary-English lengths this would redact half a sentence
#: every time an agent said "the password was changed".
#: The value charset is wider here than in the patterns above, which run to end
#: of line anyway: a generated passphrase contains punctuation, and stopping at
#: the first `&` would mask nine characters of it and print the rest.
_NARRATED = re.compile(
    rf"(?i)\b({_NAME})\s+(?:is\s+|was\s+|=\s*)?([A-Za-z0-9_\-./+=!@#$%^&*~?]{{16,}})"
)

#: `Authorization: Bearer <token>` survives the colon pattern (the name is
#: "Authorization", which contains "auth", so it is actually caught) — this is
#: for a bearer token quoted anywhere else.
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-./+=]{8,})")

#: A key block is unmistakable and is never wanted in a summary.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: Provider credentials, which look like nothing else and are worth catching by
#: shape even when they are printed with no name attached.
_KEYISH = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,})")

#: Below this a "secret" is not usefully secret, and masking short strings turns
#: ordinary text ("password: no") into noise.
_MIN_LITERAL = 8


def _literals() -> list[str]:
    """Values this process holds that must never appear in agent-bound text.

    Read on every call rather than cached at import: the tests and the key
    rotation path both change settings underneath a running process, and a
    cached list would keep masking the old value and miss the new one.
    """
    found: list[str] = []
    for value in (
        settings.secret_key,
        settings.jwt_secret,
        settings.admin_password,
        settings.database_url,
    ):
        text = (value or "").strip()
        # "change-me-please" is the shipped jwt_secret default. Masking it would
        # redact the word out of documentation and prove nothing.
        if len(text) >= _MIN_LITERAL and text != "change-me-please":
            found.append(text)
    # Longest first, so a value that contains another (the database URL contains
    # its own password) is masked whole rather than leaving a fragment behind.
    return sorted(set(found), key=len, reverse=True)


def redact(text: str | None) -> str:
    """The text with anything that looks like a secret replaced by MASK."""
    if not text:
        return ""
    out = _PEM.sub(f"{MASK} (private key removed)", text)
    out = _KEYISH.sub(MASK, out)
    out = _ASSIGNMENT.sub(lambda m: m.group(1) + MASK, out)
    out = _COLON.sub(lambda m: m.group(1) + MASK, out)
    out = _INLINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _BEARER.sub(lambda m: m.group(1) + MASK, out)
    out = _NARRATED.sub(lambda m: f"{m.group(1)} {MASK}", out)
    for literal in _literals():
        out = out.replace(literal, MASK)
    return out


#: Commands whose whole point is to print the environment. Their output is an
#: environment dump by definition, so it is dropped rather than pattern-matched:
#: the patterns above are good, and "good" is the wrong standard for a file that
#: exists because a real key really did leak this way.
_DUMPS_ENVIRONMENT = re.compile(
    r"(?:^|[|;&]\s*|\bsudo\s+)(?:env|printenv|set|export|declare\s+-x|"
    r"cat\s+/proc/\d+/environ|cat\s+[^\s|;&]*\.env)\b(?!\s*\w+=)",
    re.IGNORECASE,
)


def dumps_environment(command: str | None) -> bool:
    """True when this command line exists to print an environment."""
    return bool(command) and bool(_DUMPS_ENVIRONMENT.search(command))
