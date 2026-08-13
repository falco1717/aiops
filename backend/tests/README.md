# Tests

Two standalone scripts, no pytest required. Both drive the app through
Starlette's `TestClient`, so the lifespan, scheduler wiring, database, runner and
websocket are all real.

Run them **from the `backend/` directory** with a throwaway SQLite database:

```bash
cd backend
export AIOPS_DATABASE_URL="sqlite+aiosqlite:///./test.db"
export AIOPS_JWT_SECRET=test AIOPS_ADMIN_PASSWORD=devpassword123
export AIOPS_WORKSPACE_ROOT="$PWD/.test-workspaces" AIOPS_COOKIE_SECURE=false

rm -f test.db && python tests/test_api.py
rm -f test.db && AIOPS_SCHEDULER_ENABLED=false python tests/test_runner.py
rm -f test_users.db && python tests/test_users.py
```

Each exits non-zero on the first failure and prints a PASS/FAIL line per check.

## `test_api.py`

Covers the HTTP surface: authentication and cookie invalidation, the workspace
path-traversal guard, preset validation (provider mismatch, illegal permission
mode, `allowed_tools` on Codex), session creation, cron and timezone validation,
schedule "run now" with session pinning, and the websocket handshake.

It also asserts the **failure** path for a run: with no `claude` binary on PATH,
a queued run must reach `failed` with a message naming the missing executable
rather than hanging.

## `test_runner.py`

Covers the parts that only appear when a process actually streams. It redirects
`ClaudeProvider.build_run` at `fake_claude_cli.py` — argv[0] only; every real
flag the provider built is preserved — and then verifies against the production
code path:

- every event kind (`system`, `thinking`, `tool_use`, `tool_result`,
  `assistant`, `result`) reaches both the websocket and the database
- token deltas stream live but are **not** persisted
- `seq` numbering is contiguous, raw payloads are retained, tool names and
  arguments are extracted
- the provider session id is captured, then reused via `--resume` on turn two
- cost and exit code land on the run row
- a second prompt during an active turn is rejected with 409
- cancellation terminates the process and settles the run as `cancelled`, with
  the session returning to `idle`
- a schedule with a real timezone (`America/Chicago`) resolves a UTC
  `next_run_at`, which is what the bundled `tzdata` is there for

The stand-in only speaks Claude's schema. Codex's `--json` output is parsed
defensively for the reasons in the root README, and is not covered here.

## `test_users.py`

Accounts, roles, and the schema migration. It builds a **pre-upgrade database**
by hand — a `users` table with no `is_admin` column, as an existing install has
— then boots the app against it and asserts the migration adds the columns *and*
promotes the existing account to admin. Getting that wrong locks the operator out
of the very screens the upgrade adds, so it is the highest-value check here.

Also covers: admin-only guards on user management and provider sign-in, the
duplicate-username and short-password rejections, the self-protection rules
(can't delete yourself, can't drop your own admin rights, can't remove the last
admin), and that a forced password change **blocks the rest of the API** rather
than just hiding the UI — with `/api/auth/*` still reachable so the user can
actually fix it.

The provider sign-in endpoints are covered at the surface level (idle status,
unknown provider, submitting a code with no flow in progress, and a clean 400
when the CLI is absent). The interactive half needs a real CLI and a human at a
browser, so it is verified by hand on the server.
