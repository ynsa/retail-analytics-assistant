# Retail analytics chat assistant: the image for reviewers and the final checks (spec 53).
#
#   docker build -t opsfleet-agent .                    # runtime image (the default target)
#   docker build --target test -t opsfleet-agent-test . # unit tests: docker run --rm opsfleet-agent-test
#
# The image contains no key and no credentials. They come from the host at run time:
# --env-file .env, and the gcloud folder mounted read only at /gcloud. README: "Run with Docker".

# Pinned by digest for repeatable builds. Update both lines together:
#   docker buildx imagetools inspect python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
# The same uv version as in .pre-commit-config.yaml and pyproject.toml (uv_build 0.13).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490

FROM ${UV_IMAGE} AS uv

# --- base: Python, the user without root rights, and the settings of all stages ------------------
FROM ${PYTHON_IMAGE} AS base
# UID 10001: no clash with a host user. On Linux, `docker run --user "$(id -u):$(id -g)"`
# replaces it, so that the files in data/ belong to the host user.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLOUDSDK_CONFIG=/gcloud \
    MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app

# --- builder: the locked dependencies, then the project (editable, so /app is the project root) --
FROM base AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# README.md is necessary: pyproject.toml names it, and uv_build reads it.
COPY pyproject.toml uv.lock README.md .python-version ./
# This layer stays in the cache when only the code changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src/ src/
# The test folders of the installed packages (pandas, numpy, pyarrow, ...) are about 75 MB.
# The assistant does not import them.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && find /app/.venv -type d -name tests -prune -exec rm -rf {} + \
 && python -m compileall -q src

# --- test: the dev tools and the unit tests (no network: -m "not live" is the default) -----------
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY config/ config/
COPY evals/ evals/
COPY tests/ tests/
USER app
# No .pytest_cache: /app is read only for the user app.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# --- runtime: the default target. No uv, no dev tools, no tests --------------------------------
FROM base AS runtime
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY config/ config/
COPY evals/ evals/
# The only folder that the assistant writes, with /tmp. Mount the host folder ./data here.
RUN mkdir -p /app/data && chown app:app /app/data
USER app
# The local UI (`ui --host 0.0.0.0`). Publish it only on the host loopback:
# -p 127.0.0.1:8000:8000.
EXPOSE 8000
ENTRYPOINT ["opsfleet-agent"]
CMD ["--help"]
