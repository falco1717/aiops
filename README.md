<img src="frontend/public/logo.svg" alt="AIOps — Make Thing Intelligent" width="360">

# AIOps

A self-hosted web control plane for coding agents. It runs the real `claude` and
`codex` CLIs on your own server and gives you a browser UI to pick which agent
and model handles each task, watch the output stream live, keep multi-turn
conversations going, point agents at git repos, and put recurring work on a cron
schedule.

Because it drives the CLIs (rather than the APIs directly), your existing Claude
and ChatGPT subscription logins are what authenticate the agents — no per-token
API billing, and you keep the CLIs' own tooling, permissions, MCP servers and
skills.

---

## ⚠️ Read this before exposing it

AIOps is, by design, **remote code execution as a service**. An authenticated
user can make an agent run arbitrary shell commands as the container's user,
inside any registered workspace. Treat the login page as the only thing between
the internet and a shell on that box:

- Put it behind TLS and a reverse proxy. Never publish it on a public port over
  plain HTTP.
- Keep `AIOPS_BIND` on loopback unless something else is terminating TLS.
- Use a long random `AIOPS_JWT_SECRET` and a real admin password.
- `AIOPS_WORKSPACE_ROOT` is a hard boundary — workspace paths that resolve
  outside it are rejected. Mount only the directories you're willing to have an
  agent modify. Don't mount `/` or your home directory.
- Provider credentials live in the container's home volumes, so anyone with
  shell access to the container can use your subscriptions.

Both CLIs authenticate as **one user**. This is a single-operator tool: adding
more AIOps accounts gives more people access to the same underlying subscription
login, which is not what per-seat subscription terms contemplate. Run one
instance per person.

---

## Architecture

```
browser ──HTTPS──> FastAPI ──subprocess──> claude -p --output-format stream-json
   │  ▲                │                   codex exec --json
   │  └── WebSocket ───┘                        │
   │      (live events)                         └── cwd = a registered workspace
   └── SPA (React) served by the same container
                        │
                        └── Postgres: sessions, runs, events, schedules, presets
```

- **Provider adapters** (`backend/app/providers/`) translate a task into CLI
  arguments and normalize each CLI's newline-delimited JSON into one event shape
  (`assistant`, `tool_use`, `tool_result`, `thinking`, `result`, …). The raw
  payload is always kept alongside the normalized form.
- **Runner** (`backend/app/runner.py`) supervises each subprocess: concurrency
  limit, timeout, cancellation (kills the whole process group, so a runaway test
  suite dies with the agent), and persistence of every event.
- **Sessions** map to a provider-side session. For Claude, AIOps assigns the
  session UUID itself via `--session-id` and resumes with `--resume`; for Codex
  the id is scraped from the event stream and reused with `codex exec resume`.
- **Scheduler** (`backend/app/scheduler.py`) polls for due cron entries every 20
  seconds and fires them, either into a fresh session per run or appending to one
  long-lived session.
- **Relay nodes** (`backend/app/relay.py`) let an agent running here reach a
  network this server cannot. A node dials out, holds a websocket open, and is
  asked to open one TCP connection at a time; a run's generated ssh config
  reaches it through a `ProxyCommand`. See *Relay nodes* below.

---

## Deploy

Prerequisites: a Linux host with Docker and Compose. Everything else — Python,
Node, `claude`, `codex`, git, ripgrep — is baked into the image.

```bash
git clone <this repo> aiops && cd aiops
cp .env.example .env
```

Fill in `.env`. At minimum:

```bash
openssl rand -hex 32   # -> AIOPS_JWT_SECRET
openssl rand -hex 24   # -> POSTGRES_PASSWORD
```

Point `AIOPS_WORKSPACES_HOST_PATH` at the directory holding the repos you want
agents to work in, set `PUBLIC_HOSTNAME` to the name your proxy will serve, then
build and start:

```bash
docker compose up -d --build
```

`docker-compose.yml` assumes an existing Traefik on a shared docker network and
publishes **no host port** — the only way in is through the proxy, which is what
keeps the forward-auth in front of it meaningful. To run without a proxy:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

If you left `AIOPS_ADMIN_PASSWORD` blank, grab the generated one:

```bash
docker compose logs app | grep -i password
```

### Sign the agent CLIs in

A fresh container has both CLIs installed but logged out, and every run fails
instantly until you fix that. Do it from the **Providers** page in the UI — no
shell needed. Sign-in requires an administrator account.

Your password never passes through AIOps. It spawns the CLI, relays what the
provider prints, and you authenticate on the provider's own site:

- **Codex** prints a link and a one-time device code. Open the link, enter the
  code, and the CLI completes by itself.
- **Claude** prints an authorize link, then waits for the authorization code
  that page gives you back — paste it into the box and AIOps hands it to the
  waiting process on stdin.

The page also reports, per provider, whether the binary is present, its version,
and whether it is signed in. Check it first whenever runs fail immediately.

Credentials persist in the `claude-home` / `codex-home` volumes, so this is a
one-time step per provider. The equivalent shell commands still work if you
prefer them:

```bash
docker compose exec -it app claude auth login
docker compose exec -it app codex login --device-auth
```

### Behind Traefik + a forward-auth

The shipped labels put AIOps behind an SSO forward-auth (Authelia by default) on
both entrypoints, so an unauthenticated request never reaches the app:

```
TRAEFIK_MIDDLEWARES=globalHeaders@file,secureHeaders@file,cloudflarewarp@docker,authelia@docker
```

Adjust the chain to match your own Traefik. Two things to keep:

- **Do not remove the forward-auth entry.** AIOps runs shell commands; its own
  login page is the second lock, not the only one.
- **Both routers get it.** Protecting only `websecure` leaves the plain-HTTP
  router as an unauthenticated path to the same service.

You still sign in twice — once to the SSO, once to AIOps. That is deliberate:
the SSO decides who may reach the box, AIOps decides who may drive an agent.
Nothing here consumes the proxy's identity headers, so a misconfigured proxy
cannot log you straight in.

Requirements on the proxy side: a DNS record for `PUBLIC_HOSTNAME` pointing at
the host, a cert resolver able to issue for that name, and the app joined to the
proxy's docker network (`TRAEFIK_NETWORK`).

#### Routing from a file instead of labels

If you keep per-app routing in Traefik's file provider, `deploy/traefik/aiops.yml`
is a ready-made config. Copy it into that directory (`/opt/traefik/` on a
Saltbox host, mounted at `/etc/traefik`) and set `TRAEFIK_ENABLE=false` in
`.env`, then recreate the container.

Turn the labels off when you do. Leaving both on means two providers serve the
same `Host` rule at equal priority, and which one wins is not deterministic.

Two syntax notes that are easy to get wrong: inside a file-provider config,
middlewares defined by that provider are referenced **bare** (`globalHeaders`),
while ones defined on docker labels keep their suffix (`gzip@docker`). And the
service URL there uses the container name rather than an IP, because the
container's address changes on every recreate.

---

## Using it

1. **Workspaces** — a workspace is a project folder on the server, and it is
   what a session's agent uses as its **working directory**: the agent starts
   there, reads and edits the files in it, and runs its commands from it. Register
   one per repo you want agents to work on. Paths are relative to `/workspaces`
   (the mount) or absolute but still confined to it. The page shows each repo's
   branch, dirty file count, and uncommitted diff.

   A session with **no** workspace runs in the workspace root with no project
   around it. That still works — it can answer questions and reach your stored
   systems — but there is no code in front of it, so "fix the bug in this repo"
   has no repo to mean.
2. **Agents** — define presets: a name plus provider, model, permission mode /
   sandbox, standing instructions, auto-approved tools, and any extra CLI flags.
   This is what "choose which agent handles this task" means in practice — pick a
   preset instead of re-entering the same six settings.
3. **Sessions** — create one against a provider/model/preset/workspace, name it
   (or leave that blank and it is named after the first task), send a task, and
   watch tool calls and output stream in. Keep talking to it; each turn resumes
   the provider-side session, so the agent keeps its context. `Ctrl+Enter` sends.
4. **Schedules** — cron entries that hand a prompt to an agent. Times are
   evaluated in the schedule's own timezone, so a 09:00 job stays at 09:00 across
   DST. "Run now" fires one immediately without disturbing the cron timing.
5. **Teams** — groups whose members all see the same sessions. Admins create
   them and decide who is in one.
6. **Account** — change your own password, and (as an admin) add, promote,
   reset, and remove users.

### Provider accounts, failover and access

A provider can have several named sign-ins — "Jordan's Claude", "Walt's Claude"
— each with its own credential directory, handed to the CLI through
`CLAUDE_CONFIG_DIR` / `CODEX_HOME`. They do not see each other. An install that
signed in before accounts existed keeps working: the existing login is adopted
as a "Default" account on upgrade.

Give an account a **fallback** and a usage limit stops being an outage: the turn
moves to the other account automatically, the run records which account served
it and where it came from, and the limited one is held out of rotation until its
window resets. Without a fallback the run fails and says so.

**Who can use what**: an account with no grants is open to everyone. Grant it to
named users and only they — plus admins — may start sessions on it. Enforced at
session creation, not just hidden in the UI.

### Usage

Two different things, deliberately kept apart:

- **Plan limits** come from the CLI itself. Claude Code emits a
  `rate_limit_event` carrying the window (`five_hour`, `weekly`), its status and
  when it resets. This is the authoritative number and is what drives automatic
  failover.
- **Measured usage** is what AIOps ran: tokens and estimated cost per window and
  per account, plus per-session context size. It covers *this server only* —
  work done in a terminal elsewhere on the same account is not counted — and
  *your own sessions only*, since a turn is only as visible as the conversation
  it belongs to. An account you can see but have not driven reads as zero rather
  than showing you somebody else's spend.

Costs are API-rate estimates. On a subscription login they are not an extra
charge; they are shown to compare relative spend.

### Subagents

`--forward-subagent-text` is on, so a subagent's text and thinking come through,
not just its tool calls. Messages carrying a `parent_tool_use_id` are folded
into a collapsible group in the transcript, so a subagent's steps read the way
they do in Claude Code rather than interleaving with the main thread.

### Skills and slash commands

These already work — type `/name` in the prompt and the CLI expands it, because
AIOps never passes `--bare`. The composer's **/ Skills** button lists what the
session actually has: skills from the workspace's `.claude/skills`, commands
from `.claude/commands`, the container's `~/.claude`, and the slash commands the
CLI itself reported at startup (so `/goal`, `/context`, `/usage` and anything a
plugin adds appear automatically). Terminal-only commands are filtered out.

### Attachments and files

Attach files to a message with the paperclip, by dropping them on the composer,
or — the one that matters — by pasting a screenshot straight out of the
clipboard. The agent is told the container path of each file and reads it from
disk; both CLIs can do that, and Claude reads images that way too. Uploads live
in their own volume, not in your workspaces, so a `git clean` in a repo cannot
take them with it.

Going the other way, the session's **Files** panel lists what is in the
directory the agent ran in and offers each file for download. The walk is
bounded — a few hundred files, a few levels deep, `.git` and dependency trees
skipped — because a workspace is usually a repo with a build tree in it. The
panel prints that rule; it does not truncate quietly.

Both directions resolve every path and require it to stay inside their root, and
downloads are always served as attachments with a content type that no browser
will execute.

### Accounts and roles

Two roles. Everyone who can sign in can drive agents; **admins** additionally
manage users and sign the provider CLIs in. The first account is created at
first boot from `AIOPS_ADMIN_*` and is an admin.

New users default to *must change password at first sign-in*. That is enforced
in the API, not just the UI: until the password is changed, every endpoint
outside `/api/auth/*` returns 403, so it cannot be skipped by calling the API
directly.

A few guard rails exist because locking yourself out of a box that runs agents is
unpleasant: you cannot delete your own account, drop your own admin rights, or
remove the last remaining admin.

Remember that an AIOps account is effectively shell access to the server through
an agent. Add accounts sparingly.

### Teams and who can see a session

A session belongs to whoever created it. It is visible to three kinds of people
and nobody else: its owner, anyone it was shared with by name, and every member
of the team it belongs to. Anything you cannot see answers **404** — not 403,
which would confirm the conversation exists.

Teams are the unit for a shared space: an admin creates one and puts people in
it, and every member sees every session that belongs to it, from the transcript
and the files down to the live event feed. Direct sharing exists alongside that
for the one-off case. The owner can also hand a session over outright, which is
how work moves on when somebody changes team.

**Approvals follow visibility.** If you can see a session you can answer the
Accept/Deny prompts a paused agent is waiting on — which is to say, you can let
it run the command it stopped on. That is the reason the sharing panel names
everyone who can see the session rather than hiding the list.

**Administrators get nothing extra**, exactly as with a stored system: being able
to administer AIOps is not the same as being entitled to read somebody's work.
They did once see every session, for a real operational reason — a session owned
by someone who has left can still hold a stopped agent, and somebody has to be
able to unstick it — but on an instance where everybody is an admin that made
every session readable by everyone. It is handled at the other end instead, when
the user is deleted:

- shared with someone by name → it goes to them; they already had it
- in a team → the team keeps it, and a remaining member takes ownership. It
  survives the team emptying out and comes back when somebody is added
- neither → deleted, with its runs and events. Nobody but the departing user
  could see it, so nothing anybody had access to is lost
- their **schedules** are deleted too. A schedule cannot be shared, so nobody
  else ever had a claim on one, and an ownerless one would keep firing prompts
  no user can read, edit or switch off

So there is nothing stranded for an admin to need to reach, and deleting a user
is not a way to inherit their work. `/api/usage` and `/api/schedules` follow the
same rule: usage counts only turns in sessions you can see, and a schedule is
visible and runnable only to its author. The one thing that stays instance-wide
is the *account roster* — every signed-in user can already see which provider
accounts exist and which are rate-limited, because you have to see them to pick
one and a limited account explains why your run failed over. The spend on them is
scoped like everything else.

**Deleting is the owner's call.** Everyone who can see a session can work in
it — send turns, rename it, change its approval mode — but a session shared into
a team is other people's work too, so only its owner (or an admin who can already
see it) can delete it or change who else is in.

### Your stored systems in somebody else's session

A turn reaches the systems **whoever asked for it** can reach — not the session
owner's. That is deliberate: you can bring your own systems into any conversation
you are a member of, and a shared session is no less capable than a private one.
Nothing about that is restricted, and the intersection rule you might expect
("only systems every viewer can reach") is deliberately *not* implemented.

What is implemented is telling you what it means, because a shared transcript
makes it non-obvious. If you prompt a session Alice can read, using a system she
cannot reach:

- everything the agent does on that host is written into a transcript she reads —
  command output, file contents, whatever is on the far end
- the decrypted private key is a file on disk for the length of the run, so
  anything that prints it puts the key itself in the transcript
- her earlier messages are context for your next turn, so **an instruction she
  left in the thread can be carried out by the agent holding your credentials**.
  She does not have to wait for the key to be printed; she can ask for the host
  to be used

So `GET /api/sessions/{id}/exposure` computes, for you, who else can read the
session and which of your own systems are reachable in it, and the chat view
shows a standing warning naming both. The first turn where that applies is
refused with **428** until you confirm it, once, and the agreement is recorded
against the exact set of people it was about — add somebody afterwards and you
are asked again, because agreeing that Bob may read what your key produces is not
agreeing that Carol may. Every turn that actually used a stored system in front of
other people is noted in the transcript, naming the systems, whose they were, who
could read the result, and when the exposure was acknowledged.

Scheduled turns are not held for a confirmation — there is nobody to ask — but the
transcript note is still written for them.

### Relay nodes

A stored system is normally dialled from this server. When the host is on a
network this server cannot reach, put a **relay node** on that network and set
the system's *Reach it via* to it. The agent still types `ssh <name>`; the
connection is made from the node instead.

The node is a jump point, not a second AIOps. `claude` and `codex` keep running
here. A node is told a host and a port, opens that one TCP connection, and
copies bytes — it never holds a provider login, an SSH key, a prompt, or
anything an agent said, and the SSH session is encrypted end-to-end between
this container and the far host regardless.

Mechanically: the run's generated ssh config gets a `ProxyCommand` that hands
its bytes to a loopback forwarder inside this container, which asks the node —
over the websocket the node dialled out on — to open the connection and dial
back with a socket for it. The node opens the far socket *first*, so its arrival
is what tells `ssh` the host answered.

- **The node dials out**, so there is no inbound rule and NAT is fine.
- **Enrolment is a one-time token**, stored hashed and spent on use. The
  credential it returns is checked on every reconnect and on every proxied
  connection, not once at enrolment.
- **A node is `pending` until an administrator approves it**, and is refused at
  the socket until then. Approving one does *not* grant the approver the right
  to route through it: a node is owned and shared exactly like a stored
  credential, and administrators get no implicit access to either.
- **Revoking is immediate** — the live connection closes and the credential
  stops authorising anything. A system bound to a revoked or missing node then
  fails to connect; it never quietly falls back to dialling direct.
- **A run can only ask for what it was given.** The forwarder accepts a
  per-run token naming the exact host and port materialised for that run.

Installers for systemd, Windows and Docker are in `deploy/relay/`, each with a
one-command uninstall. The node runs as a named service under its own account
and logs every address it is asked to connect to; it is deliberately easy to
see and easy to remove.

### Permission modes

The preset's permission mode is the single most consequential setting, because it
decides what the agent may do without asking — and nothing in a headless run can
answer a prompt, so an agent that *would* have asked simply stalls or fails.

| Provider | Value | Effect |
|---|---|---|
| claude | `default` | Only pre-approved tools run; anything else fails rather than prompting |
| claude | `acceptEdits` | File writes and common filesystem commands auto-approve |
| claude | `dontAsk` | Denies anything not explicitly allowed — the locked-down choice |
| claude | `bypassPermissions` | No checks at all |
| codex | `read-only` | No writes, no commands with side effects |
| codex | `workspace-write` | Writes confined to the working directory (the sensible default) |
| codex | `danger-full-access` | No sandbox |

Pair `acceptEdits`/`workspace-write` with a workspace that is a git repo you can
`git reset`, and you get a useful blast radius: the agent can work freely, and
you review the diff before anything leaves the box.

### Models and reasoning effort

Both CLIs let you choose how long the model thinks before it acts, and AIOps
exposes it on the session, on a preset, and in the session header. Neither list
is invented here:

- **Claude** takes `--effort <level>`, and its own help names the levels: `low`,
  `medium`, `high`, `xhigh`, `max`.
- **Codex** has no such flag. The level is a config override,
  `-c model_reasoning_effort=<level>`, and over the app-server (an interactive
  "ask" turn) it is the `effort` field of `turn/start`. The accepted levels come
  from `codex debug models`, which also **narrows them per model** — `sol` and
  `terra` go up to `ultra`, `luna` stops at `max`, and the 5.4/5.5 family stops
  at `xhigh`.

The model list shown in the UI comes from that same catalog. Both are pinned by
`backend/tests/test_effort.py`, because the failure mode is silent: Codex accepts
any string for that config key and only fails once the turn is running, and
Claude warns about an unknown level and then quietly uses its default. AIOps
therefore validates the level against the chosen model before it stores it.

A session's own effort wins; with none set it inherits its preset's; with neither
set nothing is passed and each CLI uses its own default.

---

## Configuration

Every setting is an `AIOPS_`-prefixed environment variable.

| Variable | Default | Purpose |
|---|---|---|
| `AIOPS_DATABASE_URL` | `postgresql+asyncpg://aiops:aiops@db:5432/aiops` | SQLAlchemy async URL |
| `AIOPS_JWT_SECRET` | — | **Required.** Signs session cookies |
| `AIOPS_JWT_TTL_HOURS` | `720` | Login lifetime |
| `AIOPS_ADMIN_USERNAME` | `admin` | Bootstrap account, created only when the user table is empty |
| `AIOPS_ADMIN_PASSWORD` | — | Blank generates one and logs it once |
| `AIOPS_WORKSPACE_ROOT` | `/workspaces` | Hard boundary for every workspace path |
| `AIOPS_ATTACHMENTS_ROOT` | `/attachments` | Where uploaded files are stored (its own volume) |
| `AIOPS_MAX_ATTACHMENT_BYTES` | `26214400` | Largest single upload, enforced while streaming |
| `AIOPS_SESSION_FILES_MAX` | `400` | Files the download panel will list before saying it stopped |
| `AIOPS_SESSION_FILES_MAX_DEPTH` | `3` | Directory levels that panel walks |
| `AIOPS_CLAUDE_BIN` / `AIOPS_CODEX_BIN` | `claude` / `codex` | Override CLI locations |
| `AIOPS_MAX_CONCURRENT_RUNS` | `4` | Agents allowed to run simultaneously |
| `AIOPS_RUN_TIMEOUT_SECONDS` | `3600` | Per-turn wall clock before the process group is killed |
| `AIOPS_STREAM_PARTIAL_MESSAGES` | `true` | Token-level streaming over the websocket (deltas are never persisted) |
| `AIOPS_SCHEDULER_ENABLED` | `true` | Turn the cron loop off |
| `AIOPS_SCHEDULER_TICK_SECONDS` | `20` | Poll interval for due schedules |
| `AIOPS_COOKIE_SECURE` | `true` | Set false only for plain-HTTP local use |
| `AIOPS_CORS_ORIGINS` | — | Comma-separated, for running the Vite dev server against a remote API |

---

## Local development

```bash
# backend
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
export AIOPS_DATABASE_URL="sqlite+aiosqlite:///./aiops.db"
export AIOPS_JWT_SECRET=dev AIOPS_ADMIN_PASSWORD=devpassword
export AIOPS_WORKSPACE_ROOT="$PWD/workspaces" AIOPS_COOKIE_SECURE=false
cd backend && uvicorn app.main:app --reload

# frontend (separate shell) — proxies /api and the websocket to :8000
cd frontend && npm install && npm run dev
```

SQLite is fine for development; the JSON columns and concurrent writes are why
production uses Postgres. `npm run build` writes straight into
`backend/app/static/`, which FastAPI serves as the SPA.

API docs are at `/docs` once you're signed in.

### Tests

Dependency-light scripts drive the real app through Starlette's `TestClient`.
`test_runner.py` swaps in a stand-in CLI so the subprocess supervisor, stream
parser, websocket fan-out, session resumption and cancellation are all
exercised without a signed-in provider.

The suites need two things the application does not — `openssh-client` for the
`ssh-keygen` that builds a real key pair, and `httpx` for the one suite that
runs the app under a real uvicorn:

```bash
apt-get install -y openssh-client        # system package; the image has it already
pip install -r backend/requirements-dev.txt   # pulls in requirements.txt too
```

Then, with **neither agent CLI on PATH** (several suites assert what happens
when the binary is missing):

```bash
cd backend
tests/run_all.sh                          # every suite, fresh database each

# or one at a time
export AIOPS_DATABASE_URL="sqlite+aiosqlite:///./test.db" AIOPS_JWT_SECRET=test
export AIOPS_ADMIN_PASSWORD=devpassword123 AIOPS_COOKIE_SECURE=false
export AIOPS_WORKSPACE_ROOT="$PWD/.test-workspaces"
rm -f test.db && python tests/test_api.py
rm -f test.db && AIOPS_SCHEDULER_ENABLED=false python tests/test_runner.py
```

See `backend/tests/README.md` for what each one covers.

---

## Known limitations

- **Codex's event schema is version-sensitive.** The adapter is written against
  output captured from a live `codex exec --json` (an item envelope:
  `item.started` / `item.completed` carrying an `item.type`), and
  `tests/test_codex_parser.py` pins it to those exact lines. Older shapes are
  still handled and anything unrecognised is rendered generically with the raw
  payload kept. If a Codex release changes the schema, that test fails rather
  than the UI quietly going blank.
- **Measured usage is per-server.** AIOps counts what it ran. It cannot see
  usage from your terminal or another machine on the same account, so treat the
  plan-window figure from the CLI as the real one.
- **Failover detects limits heuristically for Codex.** Claude reports plan state
  structurally; for Codex the adapter matches limit wording in the error text.
  A novel phrasing would surface as an ordinary failure rather than a failover.
- **One turn at a time per session.** Sending a prompt while a session is still
  working returns 409. Both CLIs resume from a stored session id, and
  interleaving two turns against one session id would corrupt that history.
- **Restarts abandon in-flight runs.** Agent subprocesses are children of the app
  container, so a redeploy kills them. On boot AIOps marks any run left `running`
  as failed rather than leaving it stuck. Drain before deploying if you care.
- **Single-node.** The runner and the event hub are in-process, so you cannot run
  two app replicas against one database.
- **No log rotation on events.** A long-running agent writes a row per tool call.
  Prune old sessions periodically if disk matters.
