# =============================================================================
# sense-collector — fleet build/deploy Makefile
# Source of truth for the version is the VERSION file at the repo root.
# Compose never builds; the Makefile builds the image + pushes it to the registry,
# and the dev/prod stacks pull it. Mirrors the bb-boutique fleet standard, trimmed
# for a single-service collector (no db/redis/nginx/css).
# =============================================================================

VERSION := $(shell cat VERSION 2>/dev/null || git -c safe.directory=$(CURDIR) describe --tags --always 2>/dev/null || echo "0.0.0-dev")
TIMESTAMP := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
COMMIT := $(shell git -c safe.directory=$(CURDIR) rev-parse --short HEAD 2>/dev/null || echo 'local')

# Private build/registry hosts are kept OUT of git (this is a public repo). Provide them
# locally via Makefile.local (gitignored; copy from Makefile.local.example). Included BEFORE
# the ?= defaults below so its values win; absent, the hosts stay empty and the registry
# push/deploy targets + luxarch no-op (build-local / demo-up / test-e2e still work).
-include Makefile.local

# Registry / images. REGISTRY (private) is supplied via Makefile.local; empty by default.
REGISTRY ?=
IMAGE_NAME := luxardolabs/sense-collector
DEV_IMAGE     := $(REGISTRY)/$(IMAGE_NAME):dev
VERSION_IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
IMAGE         := $(REGISTRY)/$(IMAGE_NAME):latest
# Locally-built runtime image for the local stacks (up / dev / demo) — no registry needed.
LOCAL_IMAGE   := sense-collector:local
# Public OSS image on GitHub Container Registry. EXTERNAL_REGISTRY overridable.
EXTERNAL_REGISTRY ?= ghcr.io
PUBLIC_IMAGE := $(EXTERNAL_REGISTRY)/luxardolabs/sense-collector

# ── Fleet guards (luxarch · luxlint · luxaudit) ────────────────────────────────
# Private-registry only (not on ghcr). The registry HOST is the one secret this public
# repo keeps out of tree: it is supplied via the gitignored Makefile.local, and every
# guard target skips gracefully when it is unset (so an external contributor still gets
# a working `make`). The VERSIONS are pinned here with `:=` — never `:latest` in a run
# target, so a run is reproducible and you always know which `--changelog --since` /
# `--new-rules --since` to read. Stay current via `make guard-version-check` (FATAL,
# first step of `check`) + `make guard-upgrade` — never by floating the tag.
LUXARCH_REGISTRY  ?=
LUXLINT_REGISTRY  ?= $(LUXARCH_REGISTRY)
LUXAUDIT_REGISTRY ?= $(LUXARCH_REGISTRY)

LUXARCH_VERSION  := 0.131.0
LUXLINT_VERSION  := 0.44.1
LUXAUDIT_VERSION := 0.4.0

LUXARCH_IMAGE  = $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$(LUXARCH_VERSION)
LUXLINT_IMAGE  = $(LUXLINT_REGISTRY)/luxardolabs/luxlint:$(LUXLINT_VERSION)
LUXAUDIT_IMAGE = $(LUXAUDIT_REGISTRY)/luxardolabs/luxaudit:$(LUXAUDIT_VERSION)

# Mount-only run form — the guards are READ-ONLY on /repo (never mutate what they judge).
GUARD_RUN = docker run --rm -v $(PWD):/repo

PLATFORMS ?= linux/amd64,linux/arm64

BUILD_ARGS := --build-arg BUILD_VERSION=$(VERSION) \
              --build-arg BUILD_TIMESTAMP=$(TIMESTAMP) \
              --build-arg BUILD_COMMIT=$(COMMIT)

# Cache busting: `make dev-build-push NOCACHE=1`
NOCACHE ?=
NO_CACHE_FLAG := $(if $(NOCACHE),--no-cache,)

# ANSI colors for `make help`
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
CYAN := \033[0;36m
NC := \033[0m
BOLD := \033[1m

# Lean test-DEPS image (pytest + locked deps), built from poetry.lock — NOT from :dev.
# ruff runs mount-only in the luxlint image; mypy in a python:slim tail. See Dockerfile.test.
TEST_IMAGE := sense-collector-test

# Poetry-in-docker — the build hosts carry no host poetry. A throwaway
# python:3.14-slim installs poetry into a /tmp venv with the repo mounted so the
# regenerated poetry.lock is written back to the host as the checkout owner.
REPO_UID := $(shell stat -c %u . 2>/dev/null || echo 1000)
REPO_GID := $(shell stat -c %g . 2>/dev/null || echo 1000)
# Keep in lockstep with the Dockerfile's POETRY_VERSION so poetry-in-docker matches the build.
POETRY_VERSION ?= 2.4.1
POETRY_SPEC := poetry$(if $(POETRY_VERSION),==$(POETRY_VERSION),)
POETRY_RUN := docker run --rm -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD):/work -w /work python:3.14-slim sh -c
POETRY_PIP := python -m venv /tmp/v && /tmp/v/bin/pip install -q --root-user-action=ignore $(POETRY_SPEC)

# Compose stacks (all .yml, short-form volumes). Four flavors:
#   compose.yml       collector-only -> your external InfluxDB/Grafana (.env.dev / :dev)
#   compose.prod.yml  collector-only -> external, prod (.env.prod / :latest)
#   compose.dev.yml   full LOCAL dev stack: your real Sense account + bundled InfluxDB+Grafana
#   compose.demo.yml  DEMO: fake Sense endpoint + bundled InfluxDB+Grafana (no account)
#   compose.e2e.yml   hardware-free e2e test (fake Sense + ephemeral InfluxDB) -> `make test-e2e`
RUN_DC  := docker compose -f compose.yml --env-file .env.dev
PROD_DC := docker compose -f compose.prod.yml --env-file .env.prod
DEV_DC  := docker compose -f compose.dev.yml --env-file .env.demo
DEMO_DC := docker compose -f compose.demo.yml --env-file .env.demo

# Remote prod deploy over SSH. Set the node explicitly (no fleet default).
#   make prod-deploy PROD_NODE=prod-node.example.com
PROD_NODE ?=
PROD_USER ?= root
PROD_DIR  ?= /opt/sense-collector
PROD_SSH  := ssh -o BatchMode=yes $(PROD_USER)@$(PROD_NODE)

.PHONY: help version \
        dev-build-push build-local version-build-push release release-public buildx-setup \
        docker-inspect docker-clean \
        up down restart logs ps shell \
        dev-up dev-down dev-clean dev-logs dev-ps dev-shell \
        prod-up prod-down prod-restart prod-logs prod-ps \
        demo-up demo-down demo-clean demo-logs demo-ps \
        check-prod-node prod-init prod-sync prod-deploy prod-status prod-logs-remote prod-health prod-rollback \
        poetry-lock poetry-update poetry-install \
        check guard-version-check guard-upgrade honest lint mypy format \
        test test-e2e arch plan audit status onboard-check \
        gitleaks gitleaks-staged hooks clean clean-all

.DEFAULT_GOAL := help

##@ General

help: ## Show this grouped command help
	@printf "\n$(BOLD)$(CYAN)sense-collector$(NC)  $(YELLOW)v$(VERSION) ($(COMMIT))$(NC)\n"
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@/ { printf "\n$(BOLD)$(BLUE)%s$(NC)\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_-]+:.*?## / { printf "  $(GREEN)%-24s$(NC) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n"

version: ## Show version / build info
	@echo "Version:   $(VERSION)"
	@echo "Commit:    $(COMMIT)"
	@echo "Timestamp: $(TIMESTAMP)"
	@echo "Dev:       $(DEV_IMAGE)"
	@echo "Release:   $(VERSION_IMAGE)  +  $(IMAGE)"
	@echo "Public:    $(PUBLIC_IMAGE):$(VERSION)  +  :latest"

##@ Docker — Build & Registry

# ONE shared fleet buildx builder — never a per-project `<repo>-builder`. A per-project
# builder holds a completely separate cache: no base-layer sharing, its own pip/npm cache
# mounts, unbounded growth. Ten repos with ten builders = ten copies of the same base
# layers and ten idle buildkit daemons (measured: ~72G, deduplicating to ~10-15G on one
# shared builder) — and the shared one gives cross-project cache hits, so builds get
# FASTER, not just smaller. Cap it with a buildkitd GC policy; see --doc
# FLEET-BUILD-DEPLOY-STANDARD.
BUILDX_BUILDER ?= luxardo-builder

# The shared builder is created ONCE and then lives forever, so without a buildkit GC
# policy it grows without bound (the 72G trap) — a canonical NAME says nothing about
# self-pruning. The config caps it at 20G and lets buildkit self-prune; it is seeded here
# if absent so a fresh host can never create an uncapped builder.
BUILDKITD_CONFIG ?= $(HOME)/.docker/buildkitd.toml

buildx-setup: ## Ensure the shared, GC-capped fleet buildx builder exists (multi-arch release builds)
	@if [ ! -f "$(BUILDKITD_CONFIG)" ]; then \
	  mkdir -p "$$(dirname "$(BUILDKITD_CONFIG)")"; \
	  printf '[worker.oci]\n  gc = true\n  [[worker.oci.gcpolicy]]\n    keepBytes = "20GB"\n    all = true\n' > "$(BUILDKITD_CONFIG)"; \
	  echo "seeded $(BUILDKITD_CONFIG) (buildkit GC capped at 20GB)"; \
	fi
	@docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 \
		|| docker buildx create --name $(BUILDX_BUILDER) --driver docker-container \
		     --buildkitd-config $(BUILDKITD_CONFIG) --use
	@docker buildx use $(BUILDX_BUILDER)

dev-build-push: ## Build + push :dev ONLY (tooling stage: dev deps + tests baked)
	docker build $(NO_CACHE_FLAG) --target dev -f Dockerfile $(BUILD_ARGS) -t $(DEV_IMAGE) .
	docker push $(DEV_IMAGE)
	@echo "Pushed $(DEV_IMAGE)"

build-local: ## Build the runtime image from CURRENT source as a local tag (no push, no registry)
	docker build $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(LOCAL_IMAGE) .

version-build-push: ## Build + push :$(VERSION) ONLY (runtime base stage) to the private registry
	docker build $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(VERSION_IMAGE) .
	docker push $(VERSION_IMAGE)
	@echo "Pushed $(VERSION_IMAGE)"

release: buildx-setup ## Build + push :$(VERSION) AND :latest (multi-arch) to the private registry
	docker buildx build $(NO_CACHE_FLAG) --target base --platform $(PLATFORMS) -f Dockerfile $(BUILD_ARGS) \
		-t $(VERSION_IMAGE) -t $(IMAGE) --push .
	@echo "Pushed $(VERSION_IMAGE) + $(IMAGE)"

release-public: ## Promote the RELEASED private image to $(EXTERNAL_REGISTRY)/luxardolabs/sense-collector by digest (run `release` first)
	docker buildx imagetools create \
		--tag $(PUBLIC_IMAGE):$(VERSION) \
		--tag $(PUBLIC_IMAGE):latest \
		$(VERSION_IMAGE)
	@echo "Promoted $(VERSION_IMAGE) -> $(PUBLIC_IMAGE):$(VERSION) + :latest (by digest — no rebuild)"

docker-inspect: ## Inspect release image metadata
	@docker inspect $(IMAGE) --format='Version: {{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || echo "Image not built"
	@docker inspect $(IMAGE) --format='Built:   {{index .Config.Labels "org.opencontainers.image.created"}}' 2>/dev/null || true
	@docker inspect $(IMAGE) --format='Commit:  {{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true

docker-clean: ## Remove local image tags (:dev, :$(VERSION), :latest)
	docker rmi $(DEV_IMAGE) $(VERSION_IMAGE) $(IMAGE) 2>/dev/null || true

##@ Collector-only — plug into your existing InfluxDB/Grafana (compose.yml, .env.dev)

up: build-local ## Build locally + start the collector against YOUR external InfluxDB (edit .env.dev)
	SENSE_IMAGE=$(LOCAL_IMAGE) $(RUN_DC) up -d
	@echo "sense-collector $(VERSION) running (collector only)"

down: ## Stop the collector
	$(RUN_DC) down

restart: ## Restart the collector
	$(RUN_DC) restart

logs: ## Follow collector logs
	$(RUN_DC) logs -f

ps: ## Collector status
	$(RUN_DC) ps

shell: ## Shell into the collector container
	$(RUN_DC) exec sense-collector /bin/bash

##@ Dev — full LOCAL stack (your real Sense account + bundled InfluxDB + Grafana)

dev-up: build-local ## Build locally + start the full dev stack (real Sense account; Grafana http://localhost:3000)
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEV_DC) up -d
	@echo "sense-collector [dev] — Grafana http://localhost:3000 (admin/admin)"

dev-down: ## Stop the dev stack (keep data volumes)
	$(DEV_DC) down

dev-clean: ## Stop the dev stack AND delete its data volumes
	$(DEV_DC) down -v

dev-logs: ## Follow dev stack logs
	$(DEV_DC) logs -f

dev-ps: ## Dev stack status
	$(DEV_DC) ps

dev-shell: ## Shell into the collector container
	$(DEV_DC) exec sense-collector /bin/bash

##@ Prod — local stack (pulls :latest, .env.prod)

prod-up: ## Pull :latest + start prod stack
	$(PROD_DC) pull
	$(PROD_DC) up -d

prod-down: ## Stop prod stack
	$(PROD_DC) down

prod-restart: ## Restart prod stack
	$(PROD_DC) restart

prod-logs: ## Follow prod logs
	$(PROD_DC) logs -f

prod-ps: ## Prod container status
	$(PROD_DC) ps

##@ Prod — remote deploy (set PROD_NODE=<host>)

check-prod-node:
	@test -n "$(PROD_NODE)" || { echo "Set PROD_NODE=<host> (e.g. make prod-deploy PROD_NODE=prod-node.example.com)"; exit 1; }

prod-init: check-prod-node ## One-time: create the output data dir on the node (owned by appuser:1000)
	$(PROD_SSH) 'mkdir -p $(PROD_DIR)/output && chown -R 1000:1000 $(PROD_DIR)/output'
	@printf "✓ output dir created on $(PROD_NODE)\n"

prod-sync: check-prod-node ## Push compose.prod.yml + .env.prod to the node (repo is source of truth)
	rsync -az --chown=1000:1000 compose.prod.yml .env.prod $(PROD_USER)@$(PROD_NODE):$(PROD_DIR)/
	@printf "✓ synced config to $(PROD_NODE):$(PROD_DIR)\n"

prod-deploy: check-prod-node ## Pull :latest + recreate the collector on the node (run release first)
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_DC) pull && $(PROD_DC) up -d'
	@printf "✓ deployed to $(PROD_NODE)\n"

prod-status: check-prod-node ## Container status on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_DC) ps'

prod-logs-remote: check-prod-node ## Follow collector logs on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_DC) logs --tail=100 -f'

prod-health: check-prod-node ## Run the in-container health check on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_DC) exec -T sense-collector python3 -m app.health.check'

prod-rollback: check-prod-node ## List image tags cached on the node for rollback
	$(PROD_SSH) 'docker images $(REGISTRY)/$(IMAGE_NAME) --format "table {{.Tag}}\t{{.CreatedAt}}"'

##@ Demo / quickstart (self-contained: collector + InfluxDB + Grafana)

demo-up: build-local ## Bring up the demo stack — FAKE Sense endpoint + auto-provisioned InfluxDB + Grafana
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEMO_DC) up -d --build
	@echo "Grafana:  http://localhost:3000  (admin/admin)  — dashboards populate from the fake Sense feed"
	@echo "InfluxDB: http://localhost:8086"

demo-down: ## Stop the demo stack (keep data volumes)
	$(DEMO_DC) down

demo-clean: ## Stop the demo stack AND delete its data volumes
	$(DEMO_DC) down -v

demo-logs: ## Follow demo stack logs
	$(DEMO_DC) logs -f

demo-ps: ## Demo stack status
	$(DEMO_DC) ps

##@ Dependencies (poetry in docker — no host poetry required)

poetry-lock: ## Generate/refresh poetry.lock from pyproject.toml (docker, no install)
	$(POETRY_RUN) '$(POETRY_PIP) && /tmp/v/bin/poetry lock'

poetry-update: ## Update deps to latest allowed + rewrite poetry.lock (docker)
	$(POETRY_RUN) '$(POETRY_PIP) && /tmp/v/bin/poetry update --lock'

poetry-install: ## Verify deps resolve + install cleanly from poetry.lock (docker, throwaway venv)
	$(POETRY_RUN) '$(POETRY_PIP) && /tmp/v/bin/poetry install --no-root --only main'

##@ Quality (lint · types · tests · secrets)

# Lean test-deps image, built from poetry.lock (NOT from :dev). Keyed on the lock + pyproject +
# Dockerfile.test, so it rebuilds ONLY when deps change — never on a code edit (source is mounted).
.test-image.stamp: poetry.lock pyproject.toml Dockerfile.test
	DOCKER_BUILDKIT=1 docker build $(NO_CACHE_FLAG) -f Dockerfile.test -t $(TEST_IMAGE) .
	@touch $@

# Every guard target is a no-op when the private registry host is unset (external
# contributor / CI without registry access) — one guard of the var, reused everywhere.
NO_REGISTRY = [ -z "$(LUXARCH_REGISTRY)" ]
SKIP_MSG    = echo "guards: LUXARCH_REGISTRY unset (see Makefile.local.example) — skipping"

check: guard-version-check honest lint mypy test arch audit gitleaks ## THE fleet gate — run before every commit

guard-version-check: ## FATAL: fail if any guard pin is behind the published latest
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	rc=0; for g in luxarch luxlint luxaudit; do \
	  pin=$$(case $$g in luxarch) echo $(LUXARCH_VERSION);; luxlint) echo $(LUXLINT_VERSION);; luxaudit) echo $(LUXAUDIT_VERSION);; esac); \
	  docker pull -q $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  if [ -n "$$latest" ] && [ "$$latest" != "$$pin" ]; then \
	    printf "✗ %s pinned %s, latest %s — behind. Preview: --new-rules --since %s; then 'make guard-upgrade'\n" "$$g" "$$pin" "$$latest" "$$pin"; rc=1; \
	  fi; done; exit $$rc

guard-upgrade: ## Bump every guard pin to the published latest (prints what newly bites)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	for g in luxarch luxlint luxaudit; do \
	  docker pull -q $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  var=$$(echo $$g | tr a-z A-Z)_VERSION; old=$$(sed -n "s/^$$var  *:= //p" Makefile); \
	  [ -n "$$latest" ] && sed -i "s|^$$var\( *\):= .*|$$var\1:= $$latest|" Makefile; \
	  [ "$$g" = luxarch ] && [ -n "$$old" ] && $(GUARD_RUN) $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$$latest --new-rules --since $$old || true; \
	done; echo "pins bumped — re-run make check"

honest: ## HONESTY gate — a green `make check` must mean nothing was silently unchecked
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	set -e; \
	$(GUARD_RUN) $(LUXARCH_IMAGE) --assert-scans; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --preflight

lint: ## ruff via luxlint (canonical config, mount-only — the repo installs nothing)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXLINT_IMAGE)

mypy: ## mypy via luxlint (fleet typed-dep union baked into the image, mount-only)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --mypy

format: ## THE canonical fixer (luxlint --format) — safe autofixes + canonical width + markdown
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	docker run --rm --user $(REPO_UID):$(REPO_GID) -v $(PWD):/repo $(LUXLINT_IMAGE) --format

# COVERAGE_FILE lives outside /app: the source mounts are read-only, so coverage cannot
# write its sqlite data file next to them. The run is piped through
# `luxlint --coverage-ratchet`, which passes the report through and then enforces the
# [test].coverage_min floor — monotonic UP, and loud (never silently green) if the
# measurement is missing. PIPESTATUS keeps pytest's own exit code authoritative: a failing
# suite must fail `make test` even when the ratchet is satisfied.
test: .test-image.stamp ## Canonical pytest suite + coverage floor (lock-built deps image, mounted source)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --emit-config pytest > .luxlint.pytest.ini; \
	set -o pipefail; \
	docker run --rm -w /app -e COVERAGE_FILE=/tmp/.coverage \
	  -v $(PWD)/app:/app/app:ro -v $(PWD)/tests:/app/tests:ro \
	  -v $(PWD)/.luxlint.pytest.ini:/cfg/pytest.ini:ro $(TEST_IMAGE) \
	  pytest -c /cfg/pytest.ini -p no:cacheprovider tests -q \
	    --cov=app --cov-report=term-missing \
	  | $(GUARD_RUN) -i $(LUXLINT_IMAGE) --coverage-ratchet; rc=$$?; \
	rm -f .luxlint.pytest.ini; \
	exit $$rc

E2E_IMAGE := sense-collector:e2e   # local build tag — the e2e gate needs no registry
test-e2e: ## Hardware-free end-to-end test: fake Sense endpoint -> collector -> InfluxDB
	docker build $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(E2E_IMAGE) .
	SENSE_IMAGE=$(E2E_IMAGE) ./scripts/e2e-test.sh

arch: ## Architecture conformance via luxarch (pinned; reads .luxarch.toml)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXARCH_IMAGE)

plan: ## THE worklist: reds (GATED) + sweep findings (TRIAGE) + inert rules (AUDIT)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXARCH_IMAGE) --plan

audit: ## Scan pinned deps against the live OSV+PyPA vulnerability feed (luxaudit, mount-only)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXAUDIT_IMAGE)

# Regenerate the committed guard-status files. The fleet reader (fleet-status.py) reads THESE
# instead of re-running every guard on every repo, and verifies each row against HEAD — so a
# stale file reads as [STALE], never as current truth. The guards stay read-only on /repo, so
# the RECIPE (which legitimately has git + write access) does the stamping, not the guard.
# `set -e`: a failed stamp (empty/invalid --json) ABORTS — never a false "wrote".
STAMP = python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); d["commit"]=os.environ["SHA"]; d["generated_at"]=os.environ["TS"]; json.dump(d,open(sys.argv[2],"w"),indent=2)'

status: ## Regenerate the committed guard-status files (.lux*-status.json) — COMMIT them
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	set -e; export SHA=$$(git rev-parse HEAD) TS=$$(date -u +%FT%TZ); \
	t=$$(mktemp -d); \
	$(GUARD_RUN) $(LUXLINT_IMAGE)  --json > $$t/lux.json || true; $(STAMP) $$t/lux.json .luxlint-status.json; \
	$(GUARD_RUN) $(LUXARCH_IMAGE)  --json > $$t/lux.json || true; $(STAMP) $$t/lux.json .luxarch-status.json; \
	$(GUARD_RUN) $(LUXAUDIT_IMAGE) --json > $$t/lux.json || true; $(STAMP) $$t/lux.json .luxaudit-status.json; \
	rm -rf $$t; echo "wrote .lux*-status.json at $$SHA — commit them"

onboard-check: ## PROVE the repo is onboarded: all three guards on + honest + privacy wired (NOT green)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	set +e; fail=0; \
	$(GUARD_RUN) $(LUXARCH_IMAGE)  --version      >/dev/null || { echo "luxarch not wired";  fail=1; }; \
	$(GUARD_RUN) $(LUXLINT_IMAGE)  --version      >/dev/null || { echo "luxlint not wired";  fail=1; }; \
	$(GUARD_RUN) $(LUXAUDIT_IMAGE) --version      >/dev/null || { echo "luxaudit not wired"; fail=1; }; \
	$(GUARD_RUN) $(LUXARCH_IMAGE)  --assert-scans >/dev/null 2>&1 || { echo "luxarch: a rule family scanned NOTHING (hollow green) — point it at real code or remove the surface"; fail=1; }; \
	$(GUARD_RUN) $(LUXLINT_IMAGE)  --preflight    || { echo "mypy tail NOT honest (luxlint --preflight)"; fail=1; }; \
	$(GUARD_RUN) $(LUXAUDIT_IMAGE) 2>&1 | grep -q "scan could not run" && { echo "luxaudit can't scan — supply-chain blind"; fail=1; }; \
	[ -f hooks/pre-commit ] || { echo "secret git-hooks NOT wired (luxlint --emit-hooks | sh, commit hooks/)"; fail=1; }; \
	[ ! -d .github/workflows ] || { echo "public CI present — make check is the sole gate (remove .github/workflows)"; fail=1; }; \
	! grep -qE 'ruff[[:space:]]+format' Makefile 2>/dev/null || { echo "Makefile shells the formatter directly (wrong width) — use 'luxlint --format'"; fail=1; }; \
	$(MAKE) -s gitleaks >/dev/null 2>&1 || { echo "gitleaks found secrets in FULL history — the pre-commit hook only sees staged diffs; scrub before onboarding is complete"; fail=1; }; \
	[ $$fail -eq 0 ] && echo "onboard-check: all three guards on + honest + privacy wired + history clean ✓" || { echo "onboard-check FAILED"; exit 1; }

# Secret scanning — THE canonical fleet gitleaks config (gitleaks defaults + the org denylist for
# internal infra / retired identity) is emitted from the luxlint image at scan time to a tmp file
# OUTSIDE the repo, then handed to gitleaks. It is NEVER committed (it names the very strings it
# forbids); luxlint's secret.no_local_gitleaks_config reds a committed .gitleaks.toml. Per-repo
# known-non-secret carve-outs live in .luxlint.toml [gitleaks].allow (mounted at emit). Skips
# gracefully when LUXLINT_REGISTRY is unset (same as lint/arch/audit).
GITLEAKS_IMAGE ?= ghcr.io/gitleaks/gitleaks:latest

gitleaks: ## Scan committed history for secrets (canonical fleet config, emitted — never committed)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	d=$$(mktemp -d); cfg=$$d/gl.toml; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --emit-config gitleaks > $$cfg; \
	docker run --rm -v $(PWD):/repo:ro -v $$cfg:/cfg/gl.toml:ro $(GITLEAKS_IMAGE) \
	  detect --source /repo --config /cfg/gl.toml --redact -v; rc=$$?; \
	rm -rf $$d; exit $$rc

gitleaks-staged: ## Pre-commit secret scan of staged changes (canonical fleet config; run before commit)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	d=$$(mktemp -d); cfg=$$d/gl.toml; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --emit-config gitleaks > $$cfg; \
	docker run --rm -v $(PWD):/repo:ro -v $$cfg:/cfg/gl.toml:ro $(GITLEAKS_IMAGE) \
	  protect --staged --source /repo --config /cfg/gl.toml --redact -v; rc=$$?; \
	rm -rf $$d; exit $$rc

hooks: ## Install the committed git hooks (hooks/) — gitleaks on every commit + push (luxlint --emit-hooks)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --emit-hooks | sh

##@ Utilities

clean: ## Clean python/test caches
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage htmlcov/

clean-all: clean docker-clean ## Clean caches + local docker image tags
