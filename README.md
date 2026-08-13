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

This is the step people miss — a fresh container has both CLIs installed but
logged out, and every run will fail instantly until you do it. Each login is
interactive and only needs doing once; credentials persist in the
`claude-home` / `codex-home` volumes.

```bash
docker compose exec -it app claude auth login
docker compose exec -it app codex login
```

On a headless server with no browser, use the device/no-browser flows:

```bash
docker compose exec -it app claude auth login   # prints a URL to open elsewhere
docker compose exec -it app codex login --device-auth
```

For Claude you can alternatively mint a long-lived token on a machine with a
browser (`claude setup-token`) and pass it in as `CLAUDE_CODE_OAUTH_TOKEN`.

The **Providers** page in the UI shows, per provider, whether the binary is
present, its version, and whether it is signed in. Check it first whenever runs
fail immediately.

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

---

## Using it

1. **Workspaces** — register a directory. Paths are relative to
   `/workspaces` (the mount) or absolute but still confined to it. The page shows
   each repo's branch, dirty file count, and uncommitted diff.
2. **Agents** — define presets: a name plus provider, model, permission mode /
   sandbox, standing instructions, auto-approved tools, and any extra CLI flags.
   This is what "choose which agent handles this task" means in practice — pick a
   preset instead of re-entering the same six settings.
3. **Sessions** — create one against a provider/model/preset/workspace, send a
   task, and watch tool calls and output stream in. Keep talking to it; each turn
   resumes the provider-side session, so the agent keeps its context. `Ctrl+Enter`
   sends.
4. **Schedules** — cron entries that hand a prompt to an agent. Times are
   evaluated in the schedule's own timezone, so a 09:00 job stays at 09:00 across
   DST. "Run now" fires one immediately without disturbing the cron timing.

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

Two dependency-light scripts drive the real app through Starlette's
`TestClient`. `test_runner.py` swaps in a stand-in CLI so the subprocess
supervisor, stream parser, websocket fan-out, session resumption and
cancellation are all exercised without a signed-in provider:

```bash
cd backend
export AIOPS_DATABASE_URL="sqlite+aiosqlite:///./test.db" AIOPS_JWT_SECRET=test
export AIOPS_ADMIN_PASSWORD=devpassword123 AIOPS_COOKIE_SECURE=false
export AIOPS_WORKSPACE_ROOT="$PWD/.test-workspaces"
rm -f test.db && python tests/test_api.py
rm -f test.db && AIOPS_SCHEDULER_ENABLED=false python tests/test_runner.py
```

See `backend/tests/README.md` for what each one covers.

---

## Known limitations

- **Codex event parsing is defensive.** Codex's `--json` schema has changed
  between releases, so the adapter recognises the shapes it knows, renders
  anything else generically, and always stores the raw payload. If a Codex run
  shows less detail than the equivalent Claude run, that's why — check a raw
  event and extend `backend/app/providers/codex.py`. Claude's `stream-json`
  schema is documented and parsed precisely.
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
