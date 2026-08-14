"""Asserts every setting the app defines can actually be set by an operator.

Twice now a setting has been added to config.py, documented as an operator
choice, and shipped unreachable: docker-compose.yml reads .env only to expand
${VAR} inside itself, and the app's `env_file=".env"` points at a path that
does not exist in the image. So a setting missing from the compose
`environment:` block silently falls back to its coded default forever, and
nothing fails — which is precisely why it needs a test rather than care.

AIOPS_SECRET_KEY hit this (credentials could not be stored at all) and
AIOPS_CODEX_INTERACTIVE_SANDBOX hit it immediately afterwards.
"""
import os
import re
import sys

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-config.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")

from app.config import Settings  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# The repo root, whether this is run from backend/ or the root.
here = os.getcwd()
compose_path = os.path.join(here, "docker-compose.yml")
if not os.path.exists(compose_path):
    compose_path = os.path.join(here, "..", "docker-compose.yml")

check("docker-compose.yml is where this test expects it", os.path.exists(compose_path), compose_path)

with open(compose_path, encoding="utf-8") as fh:
    compose = fh.read()

# Only the app service's environment block; the db service has its own.
app_block = compose.split("environment:", 1)[1] if "environment:" in compose else ""
forwarded = set(re.findall(r"^\s{6}(AIOPS_[A-Z0-9_]+):", app_block, re.MULTILINE))

defined = {f"AIOPS_{name.upper()}" for name in Settings.model_fields}

missing = sorted(defined - forwarded)
check(
    "every setting the app defines is forwarded into the container",
    not missing,
    f"unreachable in production: {', '.join(missing)}" if missing else "",
)

unknown = sorted(forwarded - defined)
check(
    "docker-compose forwards nothing the app does not read",
    not unknown,
    f"not a real setting: {', '.join(unknown)}" if unknown else "",
)

# The two that have actually bitten, named so a regression is unmistakable.
for critical in ("AIOPS_SECRET_KEY", "AIOPS_CODEX_INTERACTIVE_SANDBOX", "AIOPS_DEFAULT_APPROVAL_MODE"):
    check(f"{critical} is settable", critical in forwarded)

# A required secret must not acquire a default that would let it boot insecurely.
check(
    "AIOPS_JWT_SECRET still refuses to start without a value",
    ":?" in (re.search(r"AIOPS_JWT_SECRET:\s*(.+)", app_block) or [""])[0],
    "it would otherwise fall back to the placeholder in config.py",
)

check(
    "storing credentials is off unless a key is supplied, rather than defaulted",
    "${AIOPS_SECRET_KEY:-}" in app_block,
    "a generated-looking default here would encrypt with a key nobody recorded",
)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
