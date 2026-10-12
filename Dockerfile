# Job-container image for the Linux reference workflow
# (examples/workflows/ai-review-linux.yml).
#
#   docker build -t gitea-auto-reviewer:0.3.0 .
#   docker run --rm -it -v ai-review-codex:/codex-home gitea-auto-reviewer:0.3.0 codex login --device-auth
#
# The Codex login lives only in the ai-review-codex volume; the image never contains credentials.
FROM node:22-bookworm-slim

# Pin the CLIs you have audited; override with --build-arg when upgrading.
ARG CODEX_VERSION=0.146.0
ARG GITNEXUS_VERSION=1.6.12

# Debian bookworm ships Python 3.11, the minimum this package supports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@openai/codex@${CODEX_VERSION}" "gitnexus@${GITNEXUS_VERSION}" \
    && npm cache clean --force

COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN python3 -m venv /opt/reviewer \
    && /opt/reviewer/bin/pip install --no-cache-dir /src uv \
    && rm -rf /src

ENV PATH=/opt/reviewer/bin:$PATH \
    CODEX_HOME=/codex-home \
    PYTHONUTF8=1
RUN mkdir -p /codex-home

# Fail the build early if any tool is missing from PATH.
RUN gitea-auto-reviewer --help >/dev/null && codex --version && gitnexus --version && uv --version
