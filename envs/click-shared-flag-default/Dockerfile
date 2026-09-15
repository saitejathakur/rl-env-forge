# Agent-facing image for click-shared-flag-default.
#
# What is deliberately NOT in this image:
#   - .git or any VCS metadata (the future history, including the fix commit,
#     is reachable from a .git link -- the #1 leak vector for environments
#     derived from real repositories; see EXPLOIT_ANALYSIS.md E1)
#   - the hidden fail-to-pass test
#   - the grader
#   - the golden patch
#   - network access at run time
#
# Two-stage build: the builder resolves and compiles dependencies, the runtime
# stage receives only the finished virtualenv and the sealed source tree. Build
# tooling never reaches the agent.

# ---------------------------------------------------------------------------
# PINNING: a version tag is the floor, not the bar. For a buyer-facing build
# this should pin by digest so the image is byte-reproducible months from now:
#     FROM python:3.12-slim-bookworm@sha256:<digest>
# Resolve with: docker buildx imagetools inspect python:3.12-slim-bookworm
# The tag form is used here so the file builds without a preresolved digest.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Contemporary toolchain. click at this commit predates pytest 9, which
# rejects the repo's parametrize style outright and fails at collection.
# Pinning the TOOLCHAIN, not just the dependencies, is what makes an aging
# asset build at all -- the most common reason a real acquired codebase fails
# ingestion is that its era's tooling has moved on without it.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY context/pyproject.toml context/README.md ./
COPY context/src ./src

RUN pip install --no-cache-dir "pytest==8.3.5" \
 && pip install --no-cache-dir -e . \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    NO_COLOR=1 \
    TERM=dumb \
    PATH="/opt/venv/bin:$PATH"

# `patch` is required by the grader to install the hidden tests at grade time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends patch \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 agent
WORKDIR /workspace

COPY --from=builder /opt/venv /opt/venv
COPY --chown=agent:agent context/ /workspace/
COPY --chown=agent:agent instruction.md /workspace/INSTRUCTION.md

# The editable install points at the builder path; re-point it at /workspace.
RUN pip install --no-deps --no-build-isolation -e /workspace \
 && chown -R agent:agent /workspace /opt/venv

# Fail the image build if a leak ever creeps back in. Cheap, and it turns a
# silent contamination into a loud build failure.
RUN test ! -e /workspace/.git || (echo "LEAK: VCS metadata in image" && exit 1)
RUN ! grep -rq "prefers_first_default" /workspace || (echo "LEAK: hidden test in image" && exit 1)

USER agent

# Runtime contract: single container, no sidecars, no docker-in-docker.
# The harness does not honour ENTRYPOINT/CMD -- this is for local use only.
CMD ["/bin/bash"]
