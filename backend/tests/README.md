# Tests

Standalone scripts, no pytest required. They drive the app through Starlette's
`TestClient`, so the lifespan, scheduler wiring, database, runner and websocket
are all real.

## What they need installed

Three things beyond the application's own requirements, and each of them has
cost somebody an afternoon by failing halfway through a suite rather than at
the start:

```bash
# from the repository root, in a clean python:3.11-slim
apt-get update && apt-get install -y --no-install-recommends openssh-client git
pip install -r backend/requirements-dev.txt
```

* **`openssh-client`** is a system package, not a pip one. `test_targets` and
  `test_exposure` generate a real ed25519 key pair with `ssh-keygen` instead of
  pasting a fixture, and `test_isolation` and `test_relay` want an `ssh` on
  PATH to shim. The runtime image installs it already (see the `Dockerfile`),
  so this only bites in a bare Python image.
* **`git`** is likewise a system package. `test_github` clones a local bare
  repository through a real `git clone` — with the workspace-from-github
  endpoint's actual credential-helper plumbing wired in — to prove no token
  ever lands in `.git/config`, rather than trusting that claim without
  checking it. The runtime image already installs it (`_git` in
  `routers/workspaces.py` needs it too), so this also only bites in a bare
  Python image.
* **`httpx`** comes from `backend/requirements-dev.txt`, which pulls in
  `requirements.txt` as well. `test_relay` runs the app under a real uvicorn on
  a real port and needs a client that speaks to a socket rather than to an ASGI
  app in-process.

`run_all.sh` checks for both before it starts and says which is missing, rather
than letting three suites fail with a traceback about something else.

Nothing else is needed: no Playwright browsers (the browser suites exercise the
pure logic and stub the driver), and deliberately **neither agent CLI on PATH**.

### Running a suite inside the built image

`test_browser` and `test_browser_user` have checks that only run where a real
Chromium and the privilege-dropping helper exist, so they are worth running
against `aiops:local` as well — 135 and 59 checks there, against 122 and 23 in
a bare Python image.

The image does **not** carry `httpx` either (it is a test dependency, and the
runtime has no business with one), and its virtualenv is not writable by the
`node` user the container runs as, so `pip install` into it fails. Install
beside it instead, and do not pass `--user`: pip refuses that inside a venv.

```bash
docker run --rm -v /opt/aiops:/src --entrypoint /bin/sh aiops:local -c '
  cd /src/backend
  pip install -q --target /tmp/pylibs httpx==0.28.1
  export PYTHONPATH=/tmp/pylibs HOME=/tmp/h
  export AIOPS_JWT_SECRET=test AIOPS_ADMIN_PASSWORD=devpassword123
  export AIOPS_COOKIE_SECURE=false
  export AIOPS_WORKSPACE_ROOT=$(mktemp -d) AIOPS_ATTACHMENTS_ROOT=$(mktemp -d)
  export AIOPS_DATABASE_URL="sqlite+aiosqlite:///$(mktemp -d)/b.db"
  python tests/test_browser.py && python tests/test_browser_user.py'
```

Mount the **repository root**, not `backend/`: `test_browser_user` reads the
helper's source out of `deploy/runas/`. And do not add `--user 0` to get around
the venv permissions — the setuid helper refuses uid 0 outright, and eleven
checks fail in a way that looks like a real regression and is not.

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

`run_all.sh` does every suite in one go — from `backend/`, each against a fresh
database of its own, and it prints the per-suite check counts together so "all
suites pass" is one command rather than fifteen. Run it with **neither agent CLI
on PATH**: several suites assert what happens when the binary is missing, and a
real `claude` or `codex` turns those checks into live agent runs.

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

The third kind of bytes is here too: a screenshot the agent's browser took,
served from the run's own directory at `/api/runs/{id}/screenshots/{name}`. Only
the names AIOps generates resolve (`screenshot-001.png`, and not
`screenshot-1.png`, `.PNG`, a traversal, or another file in the same
directory), a symlink planted in that directory under an accepted name is
refused — the agent can write there, because reading a capture back by path is
how it looks at one — and the whole thing is asserted to disappear when the run
ends: the directory goes, and the endpoint answers 404 with a sentence saying
why. There is deliberately no second copy with a longer life.

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
that deciding one returns 404. It then walks the two ways in — a direct share and
team membership — and asserts each grants the list, the transcript and the
approval, and that withdrawing the share or the membership takes all of it away
again. Ownership transfer is checked from both ends: the new owner gains it, the
old owner loses it.

There used to be a third way in: being an administrator. That is gone, and the
same block now runs the whole outsider list against an admin who owns nothing,
holds no share and is in no team — including the live feed, which is where the
bypass survived longest. Subscribing with no `session_id` was an admin's feed of
every session at once, and the socket carries tool calls as they happen, so it
leaked more than the transcript did. It is refused for everyone now; the Sessions
page never used it, and keeps its list current by refetching over HTTP.

Then the other end of that rule, which is what makes removing the bypass safe.
Deleting a user must leave nothing behind that nobody can see:

- a session shared with someone by name goes to them, without also leaving them
  holding a share of what is now their own session
- a session in a team stays with the team, owned by a member who is still there —
  and survives the team emptying out entirely, coming back when somebody is added
- a session with neither is destroyed, with its runs and its events
- their schedules go too, because a schedule cannot be shared and an ownerless
  one keeps firing prompts nobody can read or switch off

The summary assertion is `stranded_sessions()`, asked of the database directly
rather than through the API — the point of a stranded session is that no user
could see one to report it. It also deletes a user who is in a team and holds a
share and asserts both rows are gone: SQLite does not enforce `ON DELETE` and
reuses integer ids, so a leftover row there is a grant waiting to be inherited by
the next account created.

Last, it boots the app a second time against the same database. The upgrade path
assigns ownerless rows to the first administrator, which is right for data that
predates ownership and wrong for the team session just left deliberately
ownerless — so "delete the owner, restart the app" must not be a way for an admin
to end up owning somebody else's work.

Scoping for the two endpoints that leaked past session visibility is here as
well. `/api/schedules` was entirely unscoped while `ScheduleOut` carries `prompt`
and `target_session_id`, so the suite asserts the list is the caller's own and
that `PUT`, `DELETE` and `/run` are 404 for anyone else, an administrator
included — firing a schedule runs commands under its author's access to stored
systems. `/api/usage` aggregated over every session; the suite asserts your own
turns are counted and somebody else's are not, and pins the one deliberate
exception: an account still appears by **name and provider**, because
`/api/accounts` shows the roster to everyone anyway, but with none of another
user's spend on it.

## `test_exposure.py`

What a shared session does with a member's own stored credentials. The rule is
permissive — a turn gets the systems its *requester* can reach, so Bob's system
works inside Alice's session even though Alice cannot reach it — and the group of
checks under "capability unchanged" exists to prove that has not been narrowed:
an intersection rule would pass everything else in the file while removing the
feature.

The rest is the disclosure around it. The exposure endpoint is checked from all
three sides — the owner's, a named sharee's, and a team member's — for naming the
right people, listing only the caller's *own* systems, and answering **404** for
somebody who was not let in. Then the acknowledgement: the first turn is refused
with **428**, agreeing to a stale audience is refused with 409, agreeing to the
real one lets the turn through, and a second turn is not held again. The re-arming
check is the point of the whole design — the owner adds Carol, and Bob is asked
once more, about Carol specifically.

The negative cases matter as much: a session nobody else can read requires nothing,
and neither does a member with no stored systems of their own — a warning shown
when there is nothing at stake is how people learn to click past the one that
matters. Last, the transcript note is asserted to exist once per turn that used a
system in front of others, to name the systems, whose they were, who could read
the result and when it was acknowledged, and to be absent from turns that used
nothing.

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

It is the suite that needs `httpx` (see "What they need installed" above), and
it wants an `ssh` on PATH for the ProxyCommand half.

## `test_effort.py`

Naming a session on creation, and reasoning effort from the API down to the
argv. Both are easy to half-wire in opposite directions, so the suite walks the
whole path rather than the endpoint: the title survives the first turn (which
used to overwrite it) and a blank one still falls back to the opening prompt;
the effort round-trips through the session, through a preset, and through the
`session ?? preset` fallback the runner reads.

The checks with teeth are the argv ones and the refusals. `claude --effort` only
*warns* on a level it does not know and then uses its default, and Codex accepts
`-c model_reasoning_effort=<anything>` at config-parse time and fails only once
the turn is running — so a level the chosen model does not accept has to be
refused by the API or it looks like it worked and silently runs at something
else. `gpt-5.5` with `ultra` is the case that pins that.

The model list is pinned against `codex debug models` on codex-cli 0.147.0.
Before this, the hardcoded list named three models that catalog has never heard
of; the UI offered them and every run using one failed.

## `test_internal_exposure.py`

Who can reach `/api/internal/*`. The other suite here whose bug was invisible
to all the rest, and for a structural reason: every suite drives the app
in-process, where there is no network for a route to be exposed on. The two
internal routers said in their docstrings that they were unreachable from
outside the container; they were mounted on the public app, and
`POST /api/internal/browser/credential` — which returns a stored system
password in plaintext — was answering `401 Unknown or expired run token` to the
open internet, turning a leaked run token into a remotely usable one.

So this suite builds the **production ASGI stack** (`loopback.build_asgi`,
which is what `uvicorn app.main:asgi` serves) and presents peer addresses:
a docker-network address for "arrived via Traefik", `127.0.0.1` for "is one of
the bridges". Off-box gets a 404 on all five internal routes and the same body
a genuinely missing route gives; loopback still gets the route.

The forged-header half is the point. Uvicorn's `ProxyHeadersMiddleware`
rewrites `scope["client"]` from `X-Forwarded-For`, left-most entry, so
`request.client.host` reads `127.0.0.1` for anybody who says so — the suite
demonstrates that on a probe app first, so nobody later "simplifies" the gate
into the attribute that does not work. It also asserts the check fails closed
when the stack is assembled without the raw-peer layer, and sweeps the whole
route table for anything else answering an anonymous caller.

## `test_static.py`

Cache headers on the built frontend — the one bug class in here that was
invisible to every other suite and shipped anyway. The app shell went out with
no `Cache-Control` and no `Expires`, which lets a browser invent a freshness
lifetime of its own (RFC 9111 §4.2.2). index.html is the only file that names
the content-hashed bundle, so an already-open browser kept loading the previous
day's JavaScript off disk and three shipped features looked broken to the
person who shipped them. Nothing failed; there was simply nothing asserting it.

So it pins both halves. Everything that can return the **shell** — `/`, a deep
link like `/sessions/<id>`, any unknown path — must carry a revalidate
directive and a validator, and answer `If-None-Match` with a 304 that *still*
carries the directive; everything under **/assets** is content-addressed and
must be `public, max-age=31536000, immutable`, with a name from an older build
answering 404 rather than falling through to the shell. It builds its own
static directory and mounts it with the application's own `mount_spa`, so it
runs in a clean clone where `app/static` does not exist.

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
