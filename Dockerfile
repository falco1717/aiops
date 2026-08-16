# syntax=docker/dockerfile:1

# ---------- stage 1: build the web UI ----------
FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# Types, then behaviour, then the bundle — cheapest signal first, and any of the
# three failing fails the build.
#
# The unit tests run here rather than in a separate step because there is no CI
# to skip: this image is what gets built and deployed, so the build is the only
# gate every change actually passes through, exactly as `tsc --noEmit` already
# relies on. They are pure functions with no DOM and no fixtures, so the cost is
# a couple of seconds on a build that spends minutes on apt and npm — cheap
# enough that "it slows every deploy" does not buy anything worth the risk of a
# check nobody runs.
#
# `npm test` rather than `npx vitest run` so the command is defined once, in
# package.json, and a developer running it by hand runs the same thing the build
# does. (The vite line stays as npx because it needs a different --outDir: the
# dev config writes into ../backend/app/static, and inside the builder we want a
# local dist/ that stage 3 can copy.)
RUN npx tsc --noEmit && npm test && npx vite build --outDir dist --emptyOutDir


# ---------- stage 2: the privilege-dropping helper ----------
# Agents run as their own user, and switching user needs a privilege the app
# deliberately does not have. This is the only thing in the image that holds
# one: a setuid binary that drops to the agent's uid and execs. Built static so
# a setuid program has no dynamic loader to be attacked through, and with the
# uids fixed at compile time so it cannot be pointed at another account.
FROM node:22-bookworm-slim AS runas
ARG AGENT_UID=1001
ARG APP_UID=1000
ARG APP_GID=1000
ARG BROWSER_UID=1002
ARG BROWSER_GID=1002
ARG BROWSER_HOME=/home/aiops-browser
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/runas/aiops-runas.c /tmp/aiops-runas.c
RUN gcc -static -O2 -Wall -Wextra -Werror \
      -DAIOPS_AGENT_UID=${AGENT_UID} \
      -DAIOPS_AGENT_GID=${APP_GID} \
      -DAIOPS_APP_UID=${APP_UID} \
      -DAIOPS_BROWSER_UID=${BROWSER_UID} \
      -DAIOPS_BROWSER_GID=${BROWSER_GID} \
      -DAIOPS_BROWSER_HOME=\"${BROWSER_HOME}\" \
      -o /tmp/aiops-runas /tmp/aiops-runas.c \
    && strip /tmp/aiops-runas


# ---------- stage 3: runtime ----------
# Node is the runtime for both agent CLIs, so it is the base image and Python
# rides along rather than the other way round.
FROM node:22-bookworm-slim AS runtime

ARG AGENT_UID=1001
ARG BROWSER_UID=1002
ARG BROWSER_GID=1002
ARG BROWSER_HOME=/home/aiops-browser

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/node \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      git \
      openssh-client \
      # Password-authenticated stored systems; the password is handed over via
      # the environment so it never appears in the process list.
      sshpass \
      ca-certificates \
      curl \
      ripgrep \
      jq \
      tini \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

# The agent CLIs AIOps drives.
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force

RUN python3 -m venv /opt/venv
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The browser an agent drives. Chromium alone — not the three Playwright
# installs by default — and into a shared path rather than a home directory,
# because the process that runs it is the agent user and the process that
# installed it is root. `a+rX` is what makes that work: the agent needs to read
# and execute the tree, and nothing needs to write to it.
#
# This is the expensive line in the image (a few hundred megabytes of browser
# and the X/graphics libraries it links against). It is a separate layer from
# everything above so a change to the application source does not rebuild it.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python3 -m playwright install --with-deps chromium \
    # `install chromium` fetches three things: the full browser, the headless
    # shell, and ffmpeg for recording video. AIOps only ever launches headless
    # and never records, and Playwright uses the shell for exactly that case —
    # so the other two are 554MB of image nobody runs. Deleted in this layer
    # rather than a later one, or they would still be carried in this one.
    # Verified both ways in the container: the shell alone launches, and with
    # seccomp=unconfined it launches *with* Chromium's own sandbox on.
    && rm -rf /opt/playwright/chromium-* /opt/playwright/ffmpeg-* \
    && rm -rf /var/lib/apt/lists/* \
    # Owned by root, run by the agent user: read and execute for everyone,
    # write for nobody.
    && chmod -R a+rX /opt/playwright

# The user every agent process runs as. It shares the application's group —
# that is how workspaces and credential directories are shared — but not its
# uid, which is what puts the app's /proc entries, memory and private files out
# of reach.
RUN useradd --uid ${AGENT_UID} --gid node --no-create-home \
      --shell /usr/sbin/nologin --comment "AIOps agent" aiops-agent

# The user Chromium runs as, and the *only* account in the image with a group
# of its own. That is the point of it. A run's SSH private keys are decrypted
# to disk group-readable by `node` because `ssh` — running as the agent — has
# to load them; anything in that group can read them. The browser renders pages
# nobody vetted, its own sandbox is off under Docker's default seccomp profile,
# and a renderer exploit is therefore code execution: before this user existed
# that code arrived at the agent's uid, holding the agent's group, next to the
# keys. Now it arrives here, where /app is unreadable, the run directory is
# unreadable, and the group grants nothing.
#
# Its home is the whole of what it may write: Playwright puts the browser
# profile and its scratch files under TMPDIR, and the helper points both HOME
# and TMPDIR here.
RUN groupadd --gid ${BROWSER_GID} aiops-browser \
    && useradd --uid ${BROWSER_UID} --gid ${BROWSER_GID} --no-create-home \
      --shell /usr/sbin/nologin --comment "AIOps browser" aiops-browser \
    && mkdir -p ${BROWSER_HOME}/tmp \
    && chown -R ${BROWSER_UID}:${BROWSER_GID} ${BROWSER_HOME} \
    && chmod 0700 ${BROWSER_HOME} ${BROWSER_HOME}/tmp

# A workspace is a bind mount whose files belong to whoever owns them on the
# host, and git refuses to read a repository owned by another uid. That check
# protects a multi-user workstation from a planted .git; here both users are
# ours and the repositories are the whole point.
RUN git config --system --add safe.directory '*'

COPY --from=runas /tmp/aiops-runas /usr/local/bin/aiops-runas
# setuid, and executable only by the application's group: the helper refuses
# any caller but the app anyway, this is the belt to that's braces.
RUN chown root:node /usr/local/bin/aiops-runas && chmod 4750 /usr/local/bin/aiops-runas

WORKDIR /app
COPY backend/app ./app
COPY --from=web /build/dist ./app/static

# The relay-node installer, served by GET /api/nodes/installer/{platform}. The
# Nodes page tells you to run `.\install.ps1` — and until this was here there
# was no way to obtain that file on the machine being installed, which on a
# fresh Windows box means nothing at all. Listed file by file rather than
# copying the directory: `deploy/relay/__pycache__` appears the moment anyone
# runs the agent out of the source tree, and a directory copy would ship it.
# The two .ps1 files are UTF-8 with a BOM and must stay byte-identical — COPY
# does not transform content, and neither does the zip built from them.
COPY deploy/relay/aiops_relay_node.py \
     deploy/relay/install.sh \
     deploy/relay/install.ps1 \
     deploy/relay/uninstall.ps1 \
     deploy/relay/aiops-relay-node.service \
     deploy/relay/Dockerfile \
     deploy/relay/docker-compose.yml \
     deploy/relay/README.md \
     ./relay/

# Three scripts in the package are run from the agent's side of the boundary:
# the MCP approval bridge and the MCP browser, both spawned by the Claude CLI,
# and the relay ProxyCommand, spawned by ssh. /app is not readable by the agent
# user, so they are installed where it can reach them. Same files, copied at
# build time, so they cannot drift from the ones the tests exercise.
RUN install -D -m 0755 /app/app/bridge/mcp_approver.py /opt/aiops-agent/mcp_approver.py \
    && install -D -m 0755 /app/app/bridge/mcp_browser.py /opt/aiops-agent/mcp_browser.py \
    && install -D -m 0755 /app/app/relay_connect.py /opt/aiops-agent/relay_connect.py

# The shim that puts the browser on the other side of the uid boundary.
#
# Playwright starts a Node driver and that driver starts Chromium, so the uid
# has to change at or above the driver — a switch below it would leave the
# driver creating the browser's profile directory as the wrong user, and would
# leave the process that parses CDP messages coming back from a compromised
# renderer running as the agent. PLAYWRIGHT_NODEJS_PATH is Playwright's own
# documented hook for "use this node instead of mine", so it is pointed at
# this: three lines that exec the setuid helper, which drops to the browser
# user, sweeps the run's credentials out of the environment and execs the real
# node. Everything below it — the driver, the browser process, every renderer —
# is that user's.
#
# The node path is asked of the installed package rather than written out, so a
# Playwright upgrade that moves it fails the build here instead of at the first
# turn that tries to browse.
RUN NODE_BIN="$(python3 -c 'from playwright._impl._driver import compute_driver_executable as c; print(c()[0])')" \
    && test -x "$NODE_BIN" \
    && printf '#!/bin/sh\n# Generated at build time. Starts Playwright as the browser user.\nexec /usr/local/bin/aiops-runas --as-browser %s "$@"\n' "$NODE_BIN" \
       > /opt/aiops-agent/browser-node \
    && chmod 0755 /opt/aiops-agent/browser-node \
    && cat /opt/aiops-agent/browser-node

# /attachments must exist here, not just at startup: Docker seeds a fresh named
# volume from the image, so a directory absent at build time gets mounted
# root-owned and the app cannot write into it. /home/node/accounts is in the
# same list for the same reason — it was missing, and a volume seeded before
# this line is still root-owned and has to be recreated by hand.
RUN mkdir -p /workspaces /attachments /home/node/.claude /home/node/.codex /home/node/accounts \
    && chown -R node:node /workspaces /attachments /home/node /app \
    # Shared with the agent user through the group: it works here, and reads
    # and writes what it is given.
    && chmod 0770 /home/node /home/node/.claude /home/node/.codex /home/node/accounts \
    && chmod 0775 /workspaces \
    && chmod 0750 /attachments \
    # The application's own source, including how the credential key is derived,
    # is the app's alone. Group is the agent's group, so it is the owner bits
    # that carry access and everything else is closed.
    && chmod -R u=rwX,go= /app

USER node
EXPOSE 8000

# Where the helpers above ended up. Set here rather than only in compose so an
# image run by hand still isolates its agents.
#
# The browser is in this image, so an image run by hand offers it too. Chromium's
# own sandbox stays off because the syscall it needs is blocked by Docker's
# default seccomp profile; see docker-compose.yml for what to change to keep it.
# Which is exactly why AIOPS_BROWSER_RUNAS matters more than that switch does:
# with Chromium's internal sandbox off, the uid it runs as is the only thing
# standing between a hostile page and this run's credentials.
ENV AIOPS_AGENT_RUNAS=/usr/local/bin/aiops-runas \
    AIOPS_AGENT_HELPER_DIR=/opt/aiops-agent \
    AIOPS_BROWSER_ENABLED=true \
    AIOPS_BROWSER_SANDBOX=off \
    AIOPS_BROWSER_RUNAS=/opt/aiops-agent/browser-node

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
