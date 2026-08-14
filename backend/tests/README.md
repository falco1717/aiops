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

## `test_attachments.py`

Files in both directions, weighted almost entirely towards paths chosen by
somebody else. Uploads: `../../etc/passwd`, `/etc/passwd` and
`..\..\windows\system32\x` are each asserted to land at the generated path
inside the attachments root and nowhere else; two uploads of `screenshot.png`
are asserted to survive independently; an oversize upload is refused and leaves
nothing on disk. Downloads: `..` traversal, its URL-encoded spellings, an
absolute path and a symlink pointing at `/etc/passwd` are all refused, and the
listing's depth and count caps are checked against real files.

It also covers the two properties that make serving user content on this origin
safe — every download is `Content-Disposition: attachment` and no upload is ever
labelled a type a browser will execute — and that every new endpoint is 401
without a session.

Set `AIOPS_ATTACHMENTS_ROOT` to a throwaway directory before running it; the
suite does that for itself, but nothing else should be pointed at the real one.

## `test_teams.py`

Session visibility, which used to be "everyone signed in sees everything". The
suite is written as the inverse of that: for each endpoint that reads or drives
a conversation — fetch, transcript, runs, events, raw events, capabilities,
files, attachments in both directions, prompt, patch, delete, per-session usage,
and the run endpoints behind them — a user who was not let in must get a **404**,
never a 403.

The checks with teeth are the approvals: answering one runs the command the agent
stopped on, so the suite asserts that an outsider's approval list is empty *and*
that deciding one returns 404. It then walks the three ways in — a direct share,
team membership, and being an administrator — and asserts each grants the list,
the transcript and the approval, and that withdrawing the share or the membership
takes all of it away again. Ownership transfer is checked from both ends: the new
owner gains it, the old owner loses it.

Last, it deletes a user who is in a team and holds a share, and asserts both rows
are gone. SQLite does not enforce `ON DELETE` and reuses integer ids, so a
leftover row there is a grant waiting to be inherited by the next account
created.

## `test_relay.py`

Relay nodes, in two halves. The first drives the API and asserts the rules: an
enrolment token works once and never again, an unapproved node is refused its
control channel, a revoked one is refused immediately and told *why*, the
credential is re-checked on every reconnect rather than trusted after
enrolment, and access matches the stored-systems model — an admin sees nothing
they were not given, and approving a node does not hand them the route through
it. It also asserts what a run's ssh config actually says: a bound system gets a
`ProxyCommand` naming its node, an unbound one does not, and a system whose node
has vanished fails rather than silently dialling direct.

The second half is the one with teeth. It runs the app under a real uvicorn on a
real port, starts the **actual agent from `deploy/relay`** as a subprocess, and
pushes bytes end to end: the ProxyCommand helper, the loopback forwarder, the
node's websocket, and a TCP listener the agent has to open on its side. It then
checks the agent cannot be pointed at an address the run was not given, and that
revoking a node mid-flight drops it. Everything in the first half would pass
against a relay that generated perfect config and moved no bytes at all.

It needs `httpx` as well as the app's own requirements.

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
