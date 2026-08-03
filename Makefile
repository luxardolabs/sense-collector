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

# Architecture guard (luxarch) — pinned, private-registry only (not on ghcr). LUXARCH_REGISTRY
# is supplied via Makefile.local; `make arch` skips gracefully when it is unset. Bump
# LUXARCH_VERSION to adopt newer rules.
LUXARCH_REGISTRY ?=
LUXARCH_VERSION  ?= 0.19.0

# Style guard (luxlint) — same private registry as luxarch, supplied out-of-tree via
# Makefile.local. LUXLINT_REGISTRY defaults to LUXARCH_REGISTRY; `make lint` skips gracefully
# when unset. luxlint owns the canonical ruff+mypy config (this repo carries none).
LUXLINT_REGISTRY ?= $(LUXARCH_REGISTRY)
LUXLINT_VERSION  ?= 0.9.0
LUXLINT_IMAGE     = $(LUXLINT_REGISTRY)/luxardolabs/luxlint:$(LUXLINT_VERSION)

# Dependency-vulnerability guard (luxaudit) — same private registry, out-of-tree via Makefile.local.
# Mount-only: reads poetry.lock, checks pinned deps against the LIVE OSV+PyPA feed (each run is
# current). LUXAUDIT_REGISTRY defaults to LUXARCH_REGISTRY; `make audit` skips gracefully when unset.
LUXAUDIT_REGISTRY ?= $(LUXARCH_REGISTRY)
LUXAUDIT_VERSION  ?= 0.1.8
LUXAUDIT_IMAGE     = $(LUXAUDIT_REGISTRY)/luxardolabs/luxaudit:$(LUXAUDIT_VERSION)
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
        lint format test test-e2e arch audit check \
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

buildx-setup: ## Ensure a buildx builder exists (multi-arch release builds)
	@docker buildx inspect sense-builder >/dev/null 2>&1 \
		|| docker buildx create --name sense-builder --use
	@docker buildx use sense-builder

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

lint: ## luxlint (ruff, mount-only) + mypy tail — ONE recipe; fails if either fails (set LUXLINT_REGISTRY in Makefile.local)
	@set +e; \
	if [ -z "$(LUXLINT_REGISTRY)" ]; then \
	  echo "luxlint: LUXLINT_REGISTRY unset (see Makefile.local.example) — skipping"; exit 0; \
	fi; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE); ruff=$$?; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config mypy > .luxlint.mypy.ini; \
	docker run --rm -e MYPYPATH=/w -v $(PWD)/.luxlint.mypy.ini:/cfg/mypy.ini:ro -v $(PWD):/w -w /w python:3.14-slim \
	  sh -c 'pip install -q mypy && mypy --config-file /cfg/mypy.ini app'; mypy=$$?; \
	rm -f .luxlint.mypy.ini; \
	if [ $$ruff -ne 0 ] || [ $$mypy -ne 0 ]; then \
	  echo "lint FAILED (luxlint=$$ruff mypy=$$mypy)"; exit 1; \
	fi

format: ## Auto-fix + format with the CANONICAL luxlint ruff config (writes back to app/)
	@if [ -z "$(LUXLINT_REGISTRY)" ]; then echo "luxlint: LUXLINT_REGISTRY unset — skipping"; exit 0; fi; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config ruff > .ruff.local.toml; \
	docker run --rm --user $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD):/w -w /w python:3.14-slim \
	  sh -c 'python -m venv /tmp/v && /tmp/v/bin/pip install -q ruff && \
	         /tmp/v/bin/ruff check --fix --config .ruff.local.toml app; \
	         /tmp/v/bin/ruff format --config .ruff.local.toml app'

test: .test-image.stamp ## Canonical pytest suite: lock-built deps image + over-mounted source (no :dev)
	@if [ -z "$(LUXLINT_REGISTRY)" ]; then \
	  echo "luxlint: LUXLINT_REGISTRY unset (see Makefile.local.example) — skipping"; exit 0; \
	fi; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config pytest > .luxlint.pytest.ini; \
	docker run --rm -w /app \
	  -v $(PWD)/app:/app/app:ro -v $(PWD)/tests:/app/tests:ro \
	  -v $(PWD)/.luxlint.pytest.ini:/cfg/pytest.ini:ro $(TEST_IMAGE) \
	  pytest -c /cfg/pytest.ini -p no:cacheprovider tests -q; rc=$$?; \
	rm -f .luxlint.pytest.ini; \
	exit $$rc

E2E_IMAGE := sense-collector:e2e   # local build tag — the e2e gate needs no registry
test-e2e: ## Hardware-free end-to-end test: fake Sense endpoint -> collector -> InfluxDB
	docker build $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(E2E_IMAGE) .
	SENSE_IMAGE=$(E2E_IMAGE) ./scripts/e2e-test.sh

arch: ## Architecture conformance via luxarch (pinned; reads .luxarch.toml — set LUXARCH_REGISTRY in Makefile.local)
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
	  echo "luxarch: LUXARCH_REGISTRY unset (see Makefile.local.example) — skipping"; \
	else docker run --rm -v $(PWD):/repo $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$(LUXARCH_VERSION); fi

audit: ## Scan pinned deps against the live OSV+PyPA vulnerability feed (luxaudit, mount-only)
	@if [ -z "$(LUXAUDIT_REGISTRY)" ]; then \
	  echo "luxaudit: LUXAUDIT_REGISTRY unset (see Makefile.local.example) — skipping"; \
	else docker run --rm -v $(PWD):/repo $(LUXAUDIT_IMAGE); fi

check: lint arch audit test gitleaks ## Run lint + arch + audit + test + secret-scan guards

# Secret scanning — THE canonical fleet gitleaks config (gitleaks defaults + the org denylist for
# internal infra / retired identity) is emitted from the luxlint image at scan time to a tmp file
# OUTSIDE the repo, then handed to gitleaks. It is NEVER committed (it names the very strings it
# forbids); luxlint's secret.no_local_gitleaks_config reds a committed .gitleaks.toml. Per-repo
# known-non-secret carve-outs live in .luxlint.toml [gitleaks].allow (mounted at emit). Skips
# gracefully when LUXLINT_REGISTRY is unset (same as lint/arch/audit).
GITLEAKS_IMAGE ?= ghcr.io/gitleaks/gitleaks:latest

gitleaks: ## Scan committed history for secrets (canonical fleet config, emitted — never committed)
	@if [ -z "$(LUXLINT_REGISTRY)" ]; then echo "luxlint: LUXLINT_REGISTRY unset (see Makefile.local.example) — skipping"; exit 0; fi; \
	d=$$(mktemp -d); cfg=$$d/gl.toml; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config gitleaks > $$cfg; \
	docker run --rm -v $(PWD):/repo:ro -v $$cfg:/cfg/gl.toml:ro $(GITLEAKS_IMAGE) \
	  detect --source /repo --config /cfg/gl.toml --redact -v; rc=$$?; \
	rm -rf $$d; exit $$rc

gitleaks-staged: ## Pre-commit secret scan of staged changes (canonical fleet config; run before commit)
	@if [ -z "$(LUXLINT_REGISTRY)" ]; then echo "luxlint: LUXLINT_REGISTRY unset — skipping"; exit 0; fi; \
	d=$$(mktemp -d); cfg=$$d/gl.toml; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config gitleaks > $$cfg; \
	docker run --rm -v $(PWD):/repo:ro -v $$cfg:/cfg/gl.toml:ro $(GITLEAKS_IMAGE) \
	  protect --staged --source /repo --config /cfg/gl.toml --redact -v; rc=$$?; \
	rm -rf $$d; exit $$rc

hooks: ## Install the committed git hooks (.githooks/) — pre-commit secret scan via gitleaks-staged
	git config core.hooksPath .githooks
	@echo "core.hooksPath -> .githooks (pre-commit runs 'make gitleaks-staged')"

##@ Utilities

clean: ## Clean python/test caches
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage htmlcov/

clean-all: clean docker-clean ## Clean caches + local docker image tags
