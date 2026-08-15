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
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/runas/aiops-runas.c /tmp/aiops-runas.c
RUN gcc -static -O2 -Wall -Wextra -Werror \
      -DAIOPS_AGENT_UID=${AGENT_UID} \
      -DAIOPS_AGENT_GID=${APP_GID} \
      -DAIOPS_APP_UID=${APP_UID} \
      -o /tmp/aiops-runas /tmp/aiops-runas.c \
    && strip /tmp/aiops-runas


# ---------- stage 3: runtime ----------
# Node is the runtime for both agent CLIs, so it is the base image and Python
# rides along rather than the other way round.
FROM node:22-bookworm-slim AS runtime

ARG AGENT_UID=1001

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

# The user every agent process runs as. It shares the application's group —
# that is how workspaces and credential directories are shared — but not its
# uid, which is what puts the app's /proc entries, memory and private files out
# of reach.
RUN useradd --uid ${AGENT_UID} --gid node --no-create-home \
      --shell /usr/sbin/nologin --comment "AIOps agent" aiops-agent

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

# Two scripts in the package are run from the agent's side of the boundary: the
# MCP approval bridge, spawned by the Claude CLI, and the relay ProxyCommand,
# spawned by ssh. /app is not readable by the agent user, so they are installed
# where it can reach them. Same files, copied at build time, so they cannot
# drift from the ones the tests exercise.
RUN install -D -m 0755 /app/app/bridge/mcp_approver.py /opt/aiops-agent/mcp_approver.py \
    && install -D -m 0755 /app/app/relay_connect.py /opt/aiops-agent/relay_connect.py

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
ENV AIOPS_AGENT_RUNAS=/usr/local/bin/aiops-runas \
    AIOPS_AGENT_HELPER_DIR=/opt/aiops-agent

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
