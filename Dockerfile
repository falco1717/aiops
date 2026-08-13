# syntax=docker/dockerfile:1

# ---------- stage 1: build the web UI ----------
FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# The dev config writes into ../backend/app/static; inside the builder we want a
# local dist/ that stage 2 can copy.
RUN npx tsc --noEmit && npx vite build --outDir dist --emptyOutDir


# ---------- stage 2: runtime ----------
# Node is the runtime for both agent CLIs, so it is the base image and Python
# rides along rather than the other way round.
FROM node:22-bookworm-slim AS runtime

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

WORKDIR /app
COPY backend/app ./app
COPY --from=web /build/dist ./app/static

RUN mkdir -p /workspaces /home/node/.claude /home/node/.codex \
    && chown -R node:node /workspaces /home/node /app

USER node
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
