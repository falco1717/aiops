"""Keeping a provider sign-in alive without ever touching the sign-in.

Two things are being defended here, and they pull in opposite directions.

The first is that a credential must not go stale unattended: a Claude OAuth
token lives eight hours and is only replaced when a turn happens to start after
it has lapsed, so an idle system does its refresh on the critical path of
whatever turn comes first — and learns there that it failed.

The second is that the cure must be incapable of causing the disease. A refresh
that AIOps performed itself would mean this process reading, rotating and
rewriting the one file whose loss forces a human to sign in again, with a
half-written file as the failure mode. So it does not: the CLI refreshes its own
credential and AIOps only decides when to give it the opportunity. Several of
the checks below exist purely to keep it that way — that the module never opens
a credential for writing, that the reader prints back no token material in any
branch, and that a refresh which fails outright leaves the bytes on disk exactly
as they were.

No real token appears anywhere in this file. The fixture credential is a
synthetic string chosen so that finding it in output is unambiguous.
"""
import asyncio
import ast
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

DB = os.path.abspath("./test_credentials.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["AIOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_SECRET_KEY", "ZmFrZS1zZWNyZXQta2V5LWZvci10ZXN0cy0wMDAwMDAwMD0=")
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
# The watch is driven by hand here; a loop ticking underneath would make every
# timing assertion a race.
os.environ["AIOPS_CREDENTIAL_WATCH_ENABLED"] = "false"
os.environ["AIOPS_COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app import credentials  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ProviderAccount, Run, Session as SessionRow  # noqa: E402
from app.schemas import AccountOut  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "app"))

#: Deliberately not shaped like a real credential. It only has to be findable.
FAKE_TOKEN = "SYNTHETIC-VALUE-THAT-IS-NOT-A-TOKEN-0123456789"


def ms(when):
    return int(when.timestamp() * 1000)


def write_credential(path, expires_at, *, refresh=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": FAKE_TOKEN,
            "refreshToken": (FAKE_TOKEN + "-refresh") if refresh else "",
            "expiresAt": ms(expires_at),
            "scopes": ["user:inference"],
            "subscriptionType": "team",
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return payload


NOW = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
LEAD = timedelta(minutes=30)
RETRY = timedelta(minutes=15)


def attempt_for(expires_at, **kw):
    return credentials.Attempt(expires_at=expires_at, **kw)


# --- 1. when to act ----------------------------------------------------
# The rule is "at most twice per credential lifetime": once inside the lead
# window, once after it has actually lapsed. Two, because a refresh token that
# has been revoked cannot be rescued by trying harder, and a loop that keeps
# trying turns one dead account into a stream of failing API calls.
expires = NOW + timedelta(hours=8)

check(
    "an account with no readable expiry is left alone",
    credentials.should_attempt(None, now=NOW, lead=LEAD, attempt=None, retry_after=RETRY)
    is False,
)
check(
    "a credential with hours left is not touched",
    credentials.should_attempt(expires, now=NOW, lead=LEAD, attempt=None, retry_after=RETRY)
    is False,
)
check(
    "one inside the lead window is",
    credentials.should_attempt(
        expires, now=expires - timedelta(minutes=20), lead=LEAD, attempt=None, retry_after=RETRY
    ),
)
check(
    "the boundary of the lead window counts as inside it",
    credentials.should_attempt(
        expires, now=expires - LEAD, lead=LEAD, attempt=None, retry_after=RETRY
    ),
)
check(
    "one second earlier does not",
    credentials.should_attempt(
        expires,
        now=expires - LEAD - timedelta(seconds=1),
        lead=LEAD,
        attempt=None,
        retry_after=RETRY,
    )
    is False,
)

tried_early = attempt_for(expires, before_expiry=True, last_at=expires - timedelta(minutes=20))
check(
    "having already tried in the lead window, it does not try again in it",
    credentials.should_attempt(
        expires,
        now=expires - timedelta(minutes=5),
        lead=LEAD,
        attempt=tried_early,
        retry_after=RETRY,
    )
    is False,
)
check(
    "but it does try once more once the credential has actually lapsed",
    credentials.should_attempt(
        expires,
        now=expires + timedelta(minutes=1),
        lead=LEAD,
        attempt=tried_early,
        retry_after=RETRY,
    ),
)

exhausted = attempt_for(
    expires, before_expiry=True, after_expiry=True, last_at=expires + timedelta(minutes=1)
)
check(
    "a credential it cannot rescue is not hammered",
    credentials.should_attempt(
        expires, now=expires + timedelta(hours=3), lead=LEAD, attempt=exhausted, retry_after=RETRY
    )
    is False,
)

errored = attempt_for(
    expires,
    before_expiry=True,
    after_expiry=True,
    last_error="probe turn timed out",
    last_at=expires + timedelta(minutes=1),
)
check(
    "an attempt that errored is retried, because an error says nothing about the credential",
    credentials.should_attempt(
        expires,
        now=expires + timedelta(minutes=20),
        lead=LEAD,
        attempt=errored,
        retry_after=RETRY,
    ),
)
check(
    "but not before the cooldown is up",
    credentials.should_attempt(
        expires,
        now=expires + timedelta(minutes=5),
        lead=LEAD,
        attempt=errored,
        retry_after=RETRY,
    )
    is False,
)

later = expires + timedelta(hours=8)
check(
    "a refreshed credential is a new lifetime with its own attempts",
    credentials.should_attempt(
        later, now=later - timedelta(minutes=5), lead=LEAD, attempt=exhausted, retry_after=RETRY
    ),
)


# --- 2. reading it without reading it ----------------------------------
parsed = credentials._state_from(
    {
        "present": True,
        "expires_at_ms": ms(expires),
        "has_refresh_token": True,
        "subscription_type": "team",
    }
)
check("an expiry in milliseconds becomes an aware datetime", parsed.expires_at == expires)
check("and is reported as known", parsed.known and parsed.has_refresh_token)

for bad in ({"expires_at_ms": "soon"}, {"expires_at_ms": True}, {"expires_at_ms": 1e30}, {}):
    state = credentials._state_from(bad)
    check(f"a nonsense expiry {bad!r} is treated as unknown, not as zero", state.expires_at is None)

check(
    "an error from the reader survives to the caller",
    credentials._state_from({"error": "no credential file"}).error == "no credential file",
)
check(
    "an over-long error is truncated rather than stored whole",
    len(credentials._state_from({"error": "x" * 900}).error) == 200,
)


# --- 3. the module is incapable of writing a credential ----------------
source = open(os.path.join(ROOT, "credentials.py"), encoding="utf-8").read()
tree = ast.parse(source)

writes = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
        mode = ""
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            writes.append(ast.dump(node)[:80])
check(
    "credentials.py never opens anything for writing",
    not writes,
    "; ".join(writes),
)
for forbidden in ("os.replace", "os.rename", "shutil.copy", "os.remove", "os.unlink"):
    check(
        f"nor does it {forbidden} — the CLI owns that file",
        forbidden not in source,
    )
check(
    "the module says out loud that it does not write credentials",
    "AIOps never writes a credential file" in source,
)


# --- 4. everything that needs a database or a subprocess ----------------
def aware(when):
    """SQLite hands back naive datetimes for a timezone-aware column.

    The same difference between the two backends that time.ts exists for on the
    front end. Nothing in the app compares a stored timestamp to `now` — the
    watch compares what it just read off disk — but the assertions here do.
    """
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


async def live_checks():
    await init_db()

    root = os.path.abspath("./.test-credentials")
    # These are credential *directories*: a file left behind by an earlier run
    # would make "this account is not signed in" quietly false.
    shutil.rmtree(root, ignore_errors=True)
    default_dir = os.path.join(root, "claude-default")
    second_dir = os.path.join(root, "claude-second")
    codex_dir = os.path.join(root, "codex-default")
    for path in (default_dir, second_dir, codex_dir):
        os.makedirs(path, exist_ok=True)

    async with SessionLocal() as db:
        first = ProviderAccount(
            name="First Claude", provider="claude", slug="first", config_dir=default_dir,
            is_default=True,
        )
        second = ProviderAccount(
            name="Second Claude", provider="claude", slug="second", config_dir=second_dir,
        )
        codex = ProviderAccount(
            name="A Codex", provider="codex", slug="codex", config_dir=codex_dir,
        )
        db.add_all([first, second, codex])
        await db.commit()
        for row in (first, second, codex):
            await db.refresh(row)
        first_id, second_id, codex_id = first.id, second.id, codex.id

    # -- 4a. the reader, run as a real subprocess ------------------------
    far = datetime.now(timezone.utc) + timedelta(hours=8)
    write_credential(os.path.join(default_dir, ".credentials.json"), far)

    async with SessionLocal() as db:
        account = await db.get(ProviderAccount, first_id)
        state = await credentials.read_state(account)
    check("a real credential file is read back", state.present and state.known, str(state.error))
    check(
        "with the expiry it actually carries",
        state.expires_at is not None
        and abs((state.expires_at - far).total_seconds()) < 1,
        str(state.expires_at),
    )
    check("and the plan it names", state.subscription_type == "team", str(state.subscription_type))
    check("and the fact that a refresh token exists", state.has_refresh_token)

    # The whole point of the boundary: what comes back must not contain the
    # credential, in any field, however it is serialised.
    blob = json.dumps(
        {
            "expires_at": str(state.expires_at),
            "subscription": state.subscription_type,
            "has_refresh": state.has_refresh_token,
            "error": state.error,
        }
    )
    check("no token material survives the read", FAKE_TOKEN not in blob, blob[:160])
    check(
        "and the reader script has no branch that could print one",
        FAKE_TOKEN not in credentials._READER
        and "accessToken" not in credentials._READER
        and "print(json.dumps(out))" in credentials._READER,
    )

    async with SessionLocal() as db:
        missing = await credentials.read_state(await db.get(ProviderAccount, second_id))
    check(
        "an account with no credential file reports that, rather than an expiry",
        missing.expires_at is None and missing.present is False,
        str(missing.error),
    )

    async with SessionLocal() as db:
        codex_row = await db.get(ProviderAccount, codex_id)
        codex_state = await credentials.read_state(codex_row)
    check(
        "a provider whose expiry AIOps does not decode reports nothing at all",
        codex_state.expires_at is None and codex_state.error is None,
        str(codex_state),
    )

    # A file that exists but is not what we expect must not become "expired".
    with open(os.path.join(second_dir, ".credentials.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    async with SessionLocal() as db:
        broken = await credentials.read_state(await db.get(ProviderAccount, second_id))
    check(
        "a corrupt credential file is an error, not a lapsed token",
        broken.expires_at is None and broken.error == "not valid JSON",
        str(broken.error),
    )
    os.remove(os.path.join(second_dir, ".credentials.json"))

    # -- 4b. a failed refresh leaves the credential exactly as it was ----
    def fake_cli(body):
        path = os.path.abspath(f"./.test-cli-{abs(hash(body)) % 10**8}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\nimport sys\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IRUSR)
        return path

    original_bin = settings.claude_bin
    credential_file = os.path.join(default_dir, ".credentials.json")
    before_bytes = open(credential_file, "rb").read()
    before_mtime = os.stat(credential_file).st_mtime_ns

    settings.claude_bin = fake_cli("sys.stderr.write('auth: refresh rejected\\n')\nsys.exit(1)")
    async with SessionLocal() as db:
        outcome = await credentials.attempt_refresh(await db.get(ProviderAccount, first_id))
    check("a CLI that refuses is reported as a failure", outcome.refreshed is False)
    check("with a reason", bool(outcome.error), str(outcome.error))
    check(
        "and the credential on disk is untouched, byte for byte",
        open(credential_file, "rb").read() == before_bytes
        and os.stat(credential_file).st_mtime_ns == before_mtime,
    )
    check(
        "the state it reports back is still the real one",
        outcome.state.expires_at is not None
        and abs((outcome.state.expires_at - far).total_seconds()) < 1,
    )

    # A CLI that is not installed at all is the other half of the same case.
    settings.claude_bin = os.path.abspath("./.no-such-cli")
    async with SessionLocal() as db:
        outcome = await credentials.attempt_refresh(await db.get(ProviderAccount, first_id))
    check(
        "a missing CLI is a failure with an explanation, not a crash",
        outcome.refreshed is False and "not installed" in (outcome.error or ""),
        str(outcome.error),
    )
    check(
        "and it too leaves the credential alone",
        open(credential_file, "rb").read() == before_bytes,
    )

    # -- 4c. a CLI that does refresh --------------------------------------
    renewed = datetime.now(timezone.utc) + timedelta(hours=16)
    settings.claude_bin = fake_cli(
        "import json, os\n"
        f"path = {credential_file!r}\n"
        "data = json.load(open(path))\n"
        f"data['claudeAiOauth']['expiresAt'] = {ms(renewed)}\n"
        "tmp = path + '.tmp'\n"
        "json.dump(data, open(tmp, 'w'))\n"
        "os.replace(tmp, path)\n"
    )
    async with SessionLocal() as db:
        outcome = await credentials.attempt_refresh(await db.get(ProviderAccount, first_id))
    check("a CLI that renews the token is recognised as having done so", outcome.refreshed)
    check(
        "and the new expiry is what gets reported",
        outcome.state.expires_at is not None
        and abs((outcome.state.expires_at - renewed).total_seconds()) < 1,
    )
    check(
        "the free check is tried before the one that costs a turn",
        outcome.method == "auth status",
        str(outcome.method),
    )

    # A CLI that succeeds but declines to renew is not a failure: it means the
    # token is still good and this was simply earlier than its own margin.
    settings.claude_bin = fake_cli("sys.exit(0)")
    async with SessionLocal() as db:
        outcome = await credentials.attempt_refresh(await db.get(ProviderAccount, first_id))
    check(
        "a CLI that declines to renew a healthy token is not an error",
        outcome.refreshed is False and outcome.error is None,
        str(outcome.error),
    )

    # -- 4d. the watch, over several accounts ----------------------------
    credentials._reset_attempts()
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    plenty = datetime.now(timezone.utc) + timedelta(hours=7)
    write_credential(os.path.join(default_dir, ".credentials.json"), soon)
    write_credential(os.path.join(second_dir, ".credentials.json"), plenty)

    calls = []
    real_attempt = credentials.attempt_refresh

    async def counting(account):
        calls.append(account.name)
        return await real_attempt(account)

    credentials.attempt_refresh = counting
    settings.claude_bin = fake_cli("sys.exit(0)")
    try:
        await credentials.refresh_due_accounts()
        check(
            "only the account that is near expiry is acted on",
            calls == ["First Claude"],
            str(calls),
        )

        async with SessionLocal() as db:
            first_row = await db.get(ProviderAccount, first_id)
            second_row = await db.get(ProviderAccount, second_id)
            codex_row = await db.get(ProviderAccount, codex_id)
            check(
                "every claude account's expiry is recorded, acted on or not",
                first_row.credential_expires_at is not None
                and second_row.credential_expires_at is not None,
                f"{first_row.credential_expires_at} / {second_row.credential_expires_at}",
            )
            check(
                "the second account's recorded expiry is its own",
                abs((aware(second_row.credential_expires_at) - plenty).total_seconds()) < 1,
            )
            check(
                "a codex account is left out of the watch entirely",
                codex_row.credential_expires_at is None
                and codex_row.credential_checked_at is None,
            )
            check("the check time is recorded", first_row.credential_checked_at is not None)

        # Second pass, same lifetime: it must not try again.
        calls.clear()
        await credentials.refresh_due_accounts()
        check("a second tick in the same lifetime does not try again", calls == [], str(calls))

        # A turn in flight owns the refresh; the watch must stand aside.
        credentials._reset_attempts()
        async with SessionLocal() as db:
            sess = SessionRow(id="s-cred", title="t", provider="claude")
            db.add(sess)
            await db.commit()
            db.add(Run(session_id=sess.id, prompt="p", status="running", account_id=first_id))
            await db.commit()
        calls.clear()
        await credentials.refresh_due_accounts()
        check(
            "an account with a turn in flight is left to that turn",
            calls == [],
            str(calls),
        )
        async with SessionLocal() as db:
            await db.execute(Run.__table__.delete())
            await db.commit()
    finally:
        credentials.attempt_refresh = real_attempt

    # -- 4e. a failing refresh is recorded, and stops after two goes ------
    credentials._reset_attempts()
    lapsed = datetime.now(timezone.utc) - timedelta(minutes=1)
    write_credential(os.path.join(default_dir, ".credentials.json"), lapsed)
    settings.claude_bin = fake_cli("sys.stderr.write('token revoked\\n')\nsys.exit(1)")
    before_bytes = open(credential_file, "rb").read()

    await credentials.refresh_due_accounts()
    async with SessionLocal() as db:
        row = await db.get(ProviderAccount, first_id)
        check("a failed refresh is recorded against the account", bool(row.credential_refresh_error))
        check(
            "and the expiry it could not renew is still shown",
            row.credential_expires_at is not None,
        )
        check("no false claim of a renewal is recorded", row.credential_refreshed_at is None)
    check(
        "a failing watch still leaves the credential file alone",
        open(credential_file, "rb").read() == before_bytes,
    )

    # -- 4f. what the API hands the browser -------------------------------
    from app.models import User as UserRow
    from app.routers.accounts import _out

    async with SessionLocal() as db:
        viewer = UserRow(username="viewer", password_hash="x", is_admin=True)
        db.add(viewer)
        await db.commit()
        await db.refresh(viewer)
        row = await db.get(ProviderAccount, first_id)
        out = await _out(row, viewer)
    check(
        "the API reports an expiry the UI can render",
        isinstance(out, AccountOut) and out.credential_expires_at is not None,
        str(out.credential_expires_at),
    )
    check(
        "it says whether AIOps is renewing this account at all",
        out.credential_watch_enabled is False,  # switched off for this suite
    )
    check(
        "it carries the failure the watch recorded",
        bool(out.credential_refresh_error),
        str(out.credential_refresh_error),
    )
    body = out.model_dump_json()
    check("and nothing in it is token material", FAKE_TOKEN not in body, body[:200])

    async with SessionLocal() as db:
        codex_out = await _out(await db.get(ProviderAccount, codex_id), viewer)
    check(
        "a codex account reports no expiry and no watch, rather than a wrong one",
        codex_out.credential_expires_at is None
        and codex_out.credential_watch_enabled is False,
    )

    settings.claude_bin = original_bin
    shutil.rmtree(root, ignore_errors=True)
    await engine.dispose()


asyncio.run(live_checks())


# --- 5. the API refuses strangers, as every other account route does ----
with TestClient(app) as client:
    r = client.get("/api/accounts")
    check("credential expiry is not readable without a session", r.status_code == 401, str(r.status_code))


# --- 6. description helpers -------------------------------------------
check(
    "a healthy credential is described by how long it has left",
    credentials.describe(
        credentials.CredentialState(present=True, expires_at=NOW + timedelta(hours=8)), now=NOW
    )
    == "valid for another 8.0h",
)
check(
    "a lapsed one says so rather than showing a negative",
    credentials.describe(
        credentials.CredentialState(present=True, expires_at=NOW - timedelta(minutes=5)), now=NOW
    )
    == "expired 5m ago",
)
check(
    "an unreadable one repeats the error",
    credentials.describe(credentials.CredentialState(error="no credential file"), now=NOW)
    == "no credential file",
)

for leftover in os.listdir("."):
    if leftover.startswith(".test-cli-"):
        os.remove(leftover)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
