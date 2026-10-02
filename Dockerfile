# =============================================================================
# sense-collector — multi-stage image (fleet standard)
#   --target base : runtime only (prod pulls :latest / :VERSION)
#   --target dev  : base + dev deps + baked tests (dev stack pulls :dev; lint/test
#                   images derive from it). See Makefile dev-build-push / release.
# All dependency versions (runtime AND dev tooling) come from poetry.lock, so the
# lint/type/test tooling is pinned and reproducible.
# =============================================================================

# ---- Stage 1: builder — resolve + install runtime deps with Poetry ----------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.4.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry into its OWN venv (pinned; pip verifies wheel hashes), never into the system
# site-packages: the base stage copies that whole tree, so a system-wide poetry shipped poetry,
# virtualenv, msgpack and setuptools into the runtime image, where luxaudit's image leg flagged
# their CVEs. With POETRY_VIRTUALENVS_CREATE=false, `poetry install` still targets the system
# interpreter, so system site-packages ends up holding the app's deps and nothing else.
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --no-cache-dir "poetry==$POETRY_VERSION" \
    && ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

# ---- Stage 1b: builder-dev — add the dev group (pytest + plugins, pinned) ----
FROM builder AS builder-dev
RUN poetry install --no-root --with dev

# ---- Stage 2: base — lean runtime image (prod) ------------------------------
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tzdata: the collector runs with TZ set (e.g. America/Chicago) and timestamps
# Sense monitor data against IANA zones — python:*-slim ships no /usr/share/zoneinfo.
# Sense uses standard IANA zone names, so (unlike python-kasa's TP-Link devices)
# tzdata-legacy is NOT needed here.
# `apt-get upgrade` pulls the Debian security fixes published since the base tag was cut —
# the OS layer (openssl, perl-base, pcre2, gzip, …) is what a lockfile cannot see.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages + console scripts from the builder (main deps only). The poetry
# symlink in /usr/local/bin would dangle here (its venv stays in the builder), so drop it.
COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# The app never runs pip, and pip vendors its own urllib3/msgpack/setuptools that no upgrade
# reaches — remove it from the runtime image.
RUN rm -f /usr/local/bin/poetry && python -m pip uninstall -y pip

WORKDIR /app

# Non-root runtime user
RUN useradd -m -u 1000 appuser

# Application code (only the package — tests/docs stay out of the runtime image)
COPY --chown=appuser:appuser app /app/app

# Own the whole workdir by appuser: the output dir must exist for a clean-clone
# build (bind-mounted at runtime, but the writer + healthcheck reference ./output
# relative to WORKDIR when unmounted), and tool caches (pytest) need it writable.
RUN mkdir -p /app/output && chown -R appuser:appuser /app

USER appuser

# Build metadata LAST so ARG churn doesn't bust the dependency layers above.
ARG BUILD_VERSION=unknown
ARG BUILD_TIMESTAMP=unknown
ARG BUILD_COMMIT=unknown
ENV SENSE_COLLECTOR_VERSION=${BUILD_VERSION} \
    SENSE_COLLECTOR_BUILD_VERSION=${BUILD_VERSION} \
    SENSE_COLLECTOR_BUILD_TIMESTAMP=${BUILD_TIMESTAMP} \
    BUILD_VERSION=${BUILD_VERSION} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP} \
    BUILD_COMMIT=${BUILD_COMMIT}
LABEL org.opencontainers.image.title="Sense Collector" \
      org.opencontainers.image.description="Sense home energy monitor metrics to InfluxDB" \
      org.opencontainers.image.source="https://github.com/luxardolabs/sense-collector" \
      org.opencontainers.image.url="https://github.com/luxardolabs/sense-collector" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.vendor="Luxardo Labs"

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "-m", "app.health.check"]

CMD ["python3", "-m", "app.main"]

# ---- Stage 3: dev — base + dev tooling + baked tests (dev stack / lint / test)
FROM base AS dev

USER root
# Overlay the dev-group site-packages (pytest + plugins, pinned by poetry.lock)
# on top of the runtime deps.
COPY --from=builder-dev /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder-dev /usr/local/bin/ /usr/local/bin/

# Bake tests + current tool config so `make test` / `make lint` are self-contained.
COPY --chown=appuser:appuser tests /app/tests
COPY --chown=appuser:appuser pyproject.toml /app/pyproject.toml

USER appuser
