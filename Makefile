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

LUXARCH_VERSION  := 0.289.3
LUXLINT_VERSION  := 0.66.0
LUXAUDIT_VERSION := 0.14.0

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

# LOCAL verification image — BARE on purpose (never pushed, never a deploy tag), built from the
# Dockerfile's `test` stage by `make test-build` on every `make test` run.
TEST_IMAGE := sense-collector:test

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
# ONE compose.yml; the four stacks are compose PROFILES, and environments differ by
# .env.<env> (never by a second compose file — repo.compose_conventions).
#   --profile dev-tyle / dev-bb    per-monitor LOCAL stack: real Sense account + bundled infra
#   --profile prod-tyle / prod-bb  per-monitor collector only -> your external InfluxDB
#   --profile demo       DEMO: fake Sense endpoint + bundled InfluxDB+Grafana (no account)
#   --profile e2e        hardware-free e2e test (fake Sense + throwaway InfluxDB) -> `make test-e2e`
DC      := docker compose
DEV_TYLE_DC := $(DC) --profile dev-tyle  --env-file .env.dev-tyle
PROD_TYLE_DC := $(DC) --profile prod-tyle --env-file .env.prod-tyle
PROD_BB_DC   := $(DC) --profile prod-bb   --env-file .env.prod-bb
DEV_BB_DC   := $(DC) --profile dev-bb    --env-file .env.dev-bb
# demo shifts off dev's Grafana port so both bundled stacks can run at once.
DEMO_DC := GRAFANA_PORT=13302 $(DC) --profile demo --env-file .env.demo
# Second Sense monitor — its own account/names/ports, credentials in gitignored .env.bb.


# Remote prod deploy over SSH. Set the node explicitly (no fleet default).
#   make prod-deploy PROD_NODE=prod-node.example.com
PROD_NODE ?=
PROD_USER ?= root
PROD_DIR  ?= /opt/sense-collector
PROD_SSH  := ssh -o BatchMode=yes $(PROD_USER)@$(PROD_NODE)

.PHONY: help version \
        dev-build-push build-local test-build harness-build version-build-push release release-public gh-release buildx-setup \
        docker-inspect docker-clean \
        up down restart logs ps shell bb-up bb-down bb-logs \
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
	@# Refuse a stray per-project builder: `buildx create` only creates, so a builder this
	@# repo (or a sibling) used before the shared one keeps running forever, uncapped, with
	@# its own cache. The orphan lives on the HOST, so only this recipe can see it.
	@# ALLOW_STRAY_BUILDERS=1 is the escape for a deliberate non-fleet builder.
	@# JSON, one builder per line: the `{{.Name}}` table form also lists NODE names
	@# (`luxardo-builder0`), which would read as a stray.
	@strays=$$(docker buildx ls --format json 2>/dev/null \
	  | sed -nE 's/.*"Name":"([^"]*)","Nodes".*/\1/p' \
	  | grep -vxE '$(BUILDX_BUILDER)|default|desktop-linux' || true); \
	if [ -n "$$strays" ] && [ -z "$(ALLOW_STRAY_BUILDERS)" ]; then \
	  echo "REFUSING: stray buildx builder(s) beside the shared $(BUILDX_BUILDER):"; \
	  echo "$$strays" | sed 's/^/    /'; \
	  echo "remove each with: docker buildx rm <name>   (or ALLOW_STRAY_BUILDERS=1 if deliberate)"; \
	  exit 1; \
	fi
	@docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 \
		|| docker buildx create --name $(BUILDX_BUILDER) --driver docker-container \
		     --buildkitd-config $(BUILDKITD_CONFIG) --use
	@docker buildx use $(BUILDX_BUILDER)

dev-build-push: ## Build + push :dev ONLY (tooling stage: dev deps + tests baked)
	docker build $(NO_CACHE_FLAG) --target dev -f Dockerfile $(BUILD_ARGS) -t $(DEV_IMAGE) .
	docker push $(DEV_IMAGE)
	@echo "Pushed $(DEV_IMAGE)"

# Compose never builds — every image is built here and referenced by tag. The fake-Sense
# emulator is no exception (--playbook compose-hygiene: "a fake-device harness is built
# outside compose too, then run by tag"). `:local` is the sanctioned never-pushed form.
HARNESS_IMAGE := sense-collector-fake:local

harness-build: ## Build the fake-Sense emulator image used by the demo + e2e profiles
	docker build $(NO_CACHE_FLAG) -t $(HARNESS_IMAGE) ./harness

test-build: ## Build the LOCAL test image from source (never pushed, never a deploy tag)
	@docker build --load --target test $(BUILD_ARGS) -f Dockerfile -t $(TEST_IMAGE) . >/dev/null

build-local: ## Build the runtime image from CURRENT source as a local tag (no push, no registry)
	docker build $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(LOCAL_IMAGE) .

# ── Publishing an immutable tag (repo.deploy_tag_is_immutable, repo.release_scans_candidate) ──
# Every target that pushes `:$(VERSION)` does three things BEFORE the push, in this order:
#   1. refuses a VERSION that is already released (fail-closed: only the registry's own
#      not-found answer reads as unreleased, so a DNS blip cannot let a re-push through);
#   2. refuses a dirty working tree (the tag names HEAD, the build reads the working dir);
#   3. scans the exact bits it is about to publish (luxaudit --image-archive) and stops on any
#      fixable HIGH/CRITICAL. `make audit` only reports what the registry already holds.
# Semantics from `luxarch --emit image-block` (v12); the shape is this repo's (multi-arch
# release + GHCR promotion), so the block itself is not pasted.
SHA_IMAGE       := $(REGISTRY)/$(IMAGE_NAME):sha-$(COMMIT)
# Multi-arch cannot be `docker save`d from a buildx push, so `release` pushes this tag (which
# nothing pins) and hands it to publish_scanned below.
CANDIDATE_IMAGE := $(REGISTRY)/$(IMAGE_NAME):candidate-$(COMMIT)

define refuse_released
	@if docker manifest inspect $(1) >/dev/null 2>&1; then \
	  echo "REFUSING: $(1) is already RELEASED. A released version is immutable: prod pins it."; \
	  echo "Bump VERSION for a new release. If its GitHub Release is missing, run: make gh-release"; \
	  exit 1; \
	fi
	@out=$$(docker manifest inspect $(1) 2>&1) || case "$$out" in \
	  *[Nn]"o such manifest"*|*"manifest unknown"*|*"not found"*) ;; \
	  *) echo "REFUSING: cannot verify $(1) is unreleased: $$out"; exit 1 ;; \
	esac
	@# A not-found only counts from a REAL registry: any plain web server's 404 (a wrong port, a
	@# misrouted proxy) also reads as "no such manifest". A registry names its API on /v2/.
	@h=$$(echo '$(1)' | cut -d/ -f1); \
	curl -sS -o /dev/null -D - --max-time 10 "https://$$h/v2/" 2>/dev/null \
	  | grep -qi '^docker-distribution-api-version:' \
	  || { echo "REFUSING: $$h does not answer as a container registry, so its not-found proves nothing"; exit 1; }
endef

define refuse_dirty_tree
	@if [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
	  echo "REFUSING: the working tree is dirty, so $(VERSION) / sha-$(COMMIT) would not describe these bits:"; \
	  git status --short | sed 's/^/    /'; \
	  echo "Commit first, then release."; \
	  exit 1; \
	fi
endef

define scan_candidate
	@set -e; ref='$(LUXAUDIT_IMAGE)'; \
	if $(NO_REGISTRY); then echo "REFUSING: set LUXARCH_REGISTRY (Makefile.local); the candidate must be scanned before it is pushed"; exit 2; fi; \
	T=$$(mktemp); trap 'rm -f "$$T"' EXIT INT TERM; \
	docker save $(1) -o "$$T"; chmod 644 "$$T"; \
	docker run --rm -v $(PWD):/repo -v luxaudit-cache:/root/.cache/trivy -v "$$T":/candidate.tar:ro \
	  "$$ref" --image-archive /candidate.tar --image-label $(1)
endef

# Multi-arch publish: $(1) is an image index already pushed, $(2) the `-t` tags to point at it.
# Everything is pinned to the index DIGEST read once up front, so a re-push of the source tag
# between scan and publish cannot swap the bits. EVERY platform image in that index is pulled by
# its own manifest digest and scanned: `docker pull` of the tag fetches the host's platform only,
# so a tag-based scan left arm64 unscanned while imagetools published it. Attestation entries
# (platform unknown/unknown) are not images and are skipped.
define publish_scanned
	@set -e; ref='$(LUXAUDIT_IMAGE)'; \
	if $(NO_REGISTRY); then echo "REFUSING: set LUXARCH_REGISTRY (Makefile.local); the candidate must be scanned before it is pushed"; exit 2; fi; \
	repo=$$(echo '$(1)' | sed -E 's/:[^/:]*$$//'); \
	index=$$(docker buildx imagetools inspect '$(1)' --format '{{json .Manifest.Digest}}' | tr -d '"'); \
	case "$$index" in sha256:*) ;; *) echo "REFUSING: could not read the index digest of $(1)"; exit 1 ;; esac; \
	plats=$$(docker buildx imagetools inspect "$$repo@$$index" --raw | python3 -c 'import json, sys; [print(m["platform"]["os"] + "/" + m["platform"]["architecture"], m["digest"]) for m in json.load(sys.stdin)["manifests"] if m.get("platform", {}).get("os", "unknown") != "unknown"]'); \
	[ -n "$$plats" ] || { echo "REFUSING: $$repo@$$index lists no platform images"; exit 1; }; \
	T=$$(mktemp); trap 'rm -f "$$T"; docker rmi sense-collector:release-scan >/dev/null 2>&1 || true' EXIT INT TERM; \
	echo "$$plats" | while read -r plat digest; do \
	  docker pull -q "$$repo@$$digest" >/dev/null; \
	  docker tag "$$repo@$$digest" sense-collector:release-scan; \
	  docker save sense-collector:release-scan -o "$$T"; chmod 644 "$$T"; \
	  docker run --rm -v $(PWD):/repo -v luxaudit-cache:/root/.cache/trivy -v "$$T":/candidate.tar:ro \
	    "$$ref" --image-archive /candidate.tar --image-label "$(1) $$plat ($$digest)" || exit 1; \
	done; \
	docker buildx imagetools create $(2) "$$repo@$$index"; \
	echo "Published $$repo@$$index (every platform scanned) as: $(2)"
endef

version-build-push: ## Build + scan + push :$(VERSION) ONLY (single-arch runtime stage) — refuses an already-released VERSION
	$(call refuse_released,$(VERSION_IMAGE))
	$(refuse_dirty_tree)
	docker build --load $(NO_CACHE_FLAG) --target base -f Dockerfile $(BUILD_ARGS) -t $(VERSION_IMAGE) .
	$(call scan_candidate,$(VERSION_IMAGE))
	docker push $(VERSION_IMAGE)
	@echo "Pushed $(VERSION_IMAGE)"

# The GitHub Release is part of cutting a release, not an afterthought: a git tag is NOT a
# Release, and without this the /releases page stays empty while release_notes/$(VERSION).md
# goes unused (repo.github_release_wired). `release` runs it last; it is idempotent, so if it
# fails after the push, re-run `make gh-release` alone. Everything it needs is checked BEFORE
# the first push, against the REMOTE: `--verify-tag` needs the tag on GitHub.
RELEASE_NOTES ?= app/release_notes/$(VERSION).md

define gh_release_preflight
	@command -v gh >/dev/null 2>&1 || { echo "REFUSING: the GitHub CLI (gh) is not installed; the release publishes its notes with it"; exit 1; }
	@gh auth status >/dev/null 2>&1 || { echo "REFUSING: gh is not authenticated (gh auth login)"; exit 1; }
	@test -f $(RELEASE_NOTES) || { echo "REFUSING: $(RELEASE_NOTES) is missing; write it before releasing"; exit 1; }
	@git ls-remote --exit-code --tags origin "refs/tags/v$(VERSION)" >/dev/null 2>&1 || { \
	  echo "REFUSING: v$(VERSION) is not on origin; the GitHub Release hangs off the pushed tag:"; \
	  echo "    git tag -a v$(VERSION) -m v$(VERSION) && git push origin v$(VERSION)"; exit 1; }
endef

gh-release: ## Publish the GitHub Release for v$(VERSION) from its release notes (idempotent; `release` runs it)
	$(gh_release_preflight)
	@if gh release view "v$(VERSION)" >/dev/null 2>&1; then \
	  echo "GitHub Release v$(VERSION) already exists, skipped (a Release, like its tag, is immutable)"; \
	else \
	  gh release create "v$(VERSION)" --verify-tag --title "$(VERSION)" --notes-file $(RELEASE_NOTES) --latest; \
	fi

release: buildx-setup ## Cut VERSION: multi-arch build, scan, then :$(VERSION) + :sha-<commit> + :latest, then the GitHub Release
	$(call refuse_released,$(VERSION_IMAGE))
	@t=$$(git rev-parse -q --verify "refs/tags/v$(VERSION)^{commit}" 2>/dev/null); \
	if [ "$$t" != "$$(git rev-parse HEAD)" ]; then \
	  echo "REFUSING: v$(VERSION) is not tagged at HEAD. Tag and push it first:"; \
	  echo "    git tag -a v$(VERSION) -m v$(VERSION) && git push origin v$(VERSION)"; exit 1; \
	fi
	$(refuse_dirty_tree)
	$(gh_release_preflight)
	docker buildx build $(NO_CACHE_FLAG) --target base --platform $(PLATFORMS) -f Dockerfile $(BUILD_ARGS) \
		-t $(CANDIDATE_IMAGE) --push .
	$(call publish_scanned,$(CANDIDATE_IMAGE),-t $(VERSION_IMAGE) -t $(SHA_IMAGE) -t $(IMAGE))
	@$(MAKE) --no-print-directory gh-release

# Promotion copies the RELEASED digest; it still probes ITS destination tag (GHCR) and re-scans
# what it is about to publish, since the feed may have moved since `release` scanned it.
release-public: ## Promote the RELEASED private image to $(EXTERNAL_REGISTRY)/luxardolabs/sense-collector by digest (run `release` first)
	$(call refuse_released,$(PUBLIC_IMAGE):$(VERSION))
	$(refuse_dirty_tree)
	$(call publish_scanned,$(VERSION_IMAGE),-t $(PUBLIC_IMAGE):$(VERSION) -t $(PUBLIC_IMAGE):latest)

docker-inspect: ## Inspect release image metadata
	@docker inspect $(IMAGE) --format='Version: {{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || echo "Image not built"
	@docker inspect $(IMAGE) --format='Built:   {{index .Config.Labels "org.opencontainers.image.created"}}' 2>/dev/null || true
	@docker inspect $(IMAGE) --format='Commit:  {{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true

docker-clean: ## Remove local image tags (:dev, :$(VERSION), :latest)
	docker rmi $(DEV_IMAGE) $(VERSION_IMAGE) $(IMAGE) 2>/dev/null || true

##@ Collector-only — plug into your existing InfluxDB/Grafana (profile collector, .env.dev)

up: build-local ## Build locally + start the collector against YOUR external InfluxDB (edit .env.dev)
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEV_TYLE_DC) up -d
	@echo "sense-collector $(VERSION) running (collector only)"

down: ## Stop the collector
	$(DEV_TYLE_DC) down

restart: ## Restart the collector
	$(DEV_TYLE_DC) restart

logs: ## Follow collector logs
	$(DEV_TYLE_DC) logs -f

ps: ## Collector status
	$(DEV_TYLE_DC) ps

shell: ## Shell into the collector container
	$(DEV_TYLE_DC) exec sense-collector /bin/bash

##@ Dev — full LOCAL stack (your real Sense account + bundled InfluxDB + Grafana)

dev-up: build-local ## Build locally + start the full dev stack (real Sense account; bundled Grafana)
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEV_TYLE_DC) up -d
	@echo "sense-collector [dev] — Grafana http://localhost:$(or $(GRAFANA_PORT),13300) (admin/admin)"

dev-down: ## Stop the dev stack (keep data volumes)
	$(DEV_TYLE_DC) down

dev-clean: ## Stop the dev stack AND delete its data volumes
	$(DEV_TYLE_DC) down -v

bb-up: build-local ## Build locally + start the SECOND monitor's stack (real Sense account)
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEV_BB_DC) up -d
	@echo "sense-collector [bb] — Grafana http://localhost:13301 (admin/admin)"

bb-down: ## Stop the bb stack (keep data volumes)
	$(DEV_BB_DC) down

bb-logs: ## Follow bb stack logs
	$(DEV_BB_DC) logs -f

dev-logs: ## Follow dev stack logs
	$(DEV_TYLE_DC) logs -f

dev-ps: ## Dev stack status
	$(DEV_TYLE_DC) ps

dev-shell: ## Shell into the collector container
	$(DEV_TYLE_DC) exec sense-collector /bin/bash

##@ Prod — local stack (pulls :latest, .env.prod)

prod-up: ## Pull :latest + start prod stack
	$(PROD_TYLE_DC) pull
	$(PROD_TYLE_DC) up -d

prod-down: ## Stop prod stack
	$(PROD_TYLE_DC) down

prod-restart: ## Restart prod stack
	$(PROD_TYLE_DC) restart

prod-logs: ## Follow prod logs
	$(PROD_TYLE_DC) logs -f

prod-ps: ## Prod container status
	$(PROD_TYLE_DC) ps

##@ Prod — remote deploy (set PROD_NODE=<host>)

check-prod-node:
	@test -n "$(PROD_NODE)" || { echo "Set PROD_NODE=<host> (e.g. make prod-deploy PROD_NODE=prod-node.example.com)"; exit 1; }

prod-init: check-prod-node ## One-time: create the output data dir on the node (owned by appuser:1000)
	$(PROD_SSH) 'mkdir -p $(PROD_DIR)/output && chown -R 1000:1000 $(PROD_DIR)/output'
	@printf "✓ output dir created on $(PROD_NODE)\n"

prod-sync: check-prod-node ## Push compose.yml + .env.prod to the node (repo is source of truth)
	rsync -az --chown=1000:1000 compose.yml .env.prod $(PROD_USER)@$(PROD_NODE):$(PROD_DIR)/
	@printf "✓ synced config to $(PROD_NODE):$(PROD_DIR)\n"

prod-deploy: check-prod-node ## Pull :latest + recreate the collector on the node (run release first)
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_TYLE_DC) pull && $(PROD_TYLE_DC) up -d'
	@printf "✓ deployed to $(PROD_NODE)\n"

prod-status: check-prod-node ## Container status on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_TYLE_DC) ps'

prod-logs-remote: check-prod-node ## Follow collector logs on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_TYLE_DC) logs --tail=100 -f'

prod-health: check-prod-node ## Run the in-container health check on the node
	$(PROD_SSH) 'cd $(PROD_DIR) && $(PROD_TYLE_DC) exec -T sense-collector python3 -m app.health.check'

prod-rollback: check-prod-node ## List image tags cached on the node for rollback
	$(PROD_SSH) 'docker images $(REGISTRY)/$(IMAGE_NAME) --format "table {{.Tag}}\t{{.CreatedAt}}"'

##@ Demo / quickstart (self-contained: collector + InfluxDB + Grafana)

demo-up: build-local harness-build ## Bring up the demo stack — FAKE Sense endpoint + auto-provisioned InfluxDB + Grafana
	SENSE_IMAGE=$(LOCAL_IMAGE) $(DEMO_DC) up -d
	@echo "Grafana:  http://localhost:3000  (admin/admin)  — dashboards populate from the fake Sense feed"

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

# luxarch:guard-upgrade asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit guard-upgrade`.
# v2: the preview mounts `$(CURDIR)`, this makefile's directory (it edits ./Makefile there). v1 mounted
# `$(PWD)`, the caller's directory, so `make -C <repo> guard-upgrade` previewed ANOTHER repo's new reds.
guard-upgrade:  ## Bump every guard pin to the published latest (prints what newly bites)
	@for g in luxarch luxlint luxaudit; do \
	  docker pull -q $(REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  var=$$(echo $$g | tr a-z A-Z)_VERSION; \
	  old=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ -z "$$old" ]; then echo "!! no $$var pin found in Makefile — NOT bumped"; continue; fi; \
	  if [ -z "$$latest" ]; then echo "!! could not read $$g:latest — $$var left at $$old"; continue; fi; \
	  checked=1; \
	  sed -i -E "s|^($$var[[:space:]]*:=[[:space:]]*).*|\\1$$latest|" Makefile; \
	  new=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ "$$new" != "$$latest" ]; then echo "!! $$var did NOT change (still $$new)"; exit 1; fi; \
	  if [ "$$old" != "$$latest" ]; then echo "$$var $$old -> $$latest"; bumped=1; fi; \
	  [ "$$g" = luxarch ] && [ "$$old" != "$$latest" ] && docker run --rm -v "$(CURDIR)":/repo $(REGISTRY)/luxardolabs/luxarch:$$latest --new-rules --since $$old || true; \
	done; \
	if [ -n "$$bumped" ]; then echo "pins bumped — re-run make check"; \
	elif [ -n "$$checked" ]; then echo "all pins already at latest"; \
	else echo "!! could not reach the registry — NO pin was checked; currency NOT established"; exit 1; fi

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

# The suite's settings for the test block below: a real InfluxDB 2.7 (the service every deployed
# stack writes to) from the compose `test` profile, its fixed test credentials from .env.e2e.
TEST_ENV_FILE := .env.e2e
TEST_SERVICES := sense_test_influxdb
TEST_ENV := -e TEST_INFLUXDB_URL=http://sense_test_influxdb:8086 -e TEST_INFLUXDB_TOKEN=sense-e2e-token \
            -e TEST_INFLUXDB_ORG=sense -e TEST_INFLUXDB_BUCKET=sense

# luxarch:test-block asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit test-block`.
# ── Test: THE suite, in the test image, against an isolated stack of real services ───────────────
# Emitted by `luxarch --emit test-block`; paste below the image block (it uses TEST_IMAGE and
# test-build from there). Enforced by `repo.test_block_wired`. Settings are `?=` defaults: set them
# above this block. What the suite can see, and what only `make smoke` sees: --doc
# FLEET-MAKEFILE-STANDARD §1.
#
# Before this block every repo wrote its own `make test`: four repos, four ways (a lint image with
# the source mounted, the dev image with pytest pip-installed at run time, a repo script, a test
# image), each with its own readiness loop and coverage wiring, and a coverage pipe under make's
# /bin/sh that hid pytest's failure. This is the documented practice of the tools instead:
#   - ISOLATED STACK (Docker Compose): the backing services run in a compose project of their own,
#     one per run (`-p`), so parallel runs never collide, the suite cannot reach the dev database or
#     cache at all (it is on another network), and teardown (`down --volumes --remove-orphans`)
#     removes exactly this run's containers and data, pass or fail, never the dev stack.
#   - READINESS from each service's own compose healthcheck (`up --wait`), not a sleep or a loop:
#     the service declares when it is ready. For Postgres, probe over TCP with the real role
#     (`pg_isready -h 127.0.0.1 -U <user> -d <db>`); over the socket it answers while initdb's
#     temporary server is still up. `repo.test_stack_parity` checks these are the services prod runs.
#   - Ctrl-C stops the suite (`--init` forwards the signal; a shell as PID 1 ignores it).
#   - THE TEST IMAGE built from this source (`test-build`, the image's `--with dev` stage), with the
#     fleet's pytest config (`luxlint --emit-config pytest`: -ra, strict markers and config,
#     warnings are errors), mounted read-only. The source is mounted read-only and pytest writes no
#     cache into it.
#   - THE RUN'S FILES stay private (v2): the run's directory keeps mktemp's 700, so no other local
#     user can reach the config pytest loads (a config can load plugins, in a container holding the
#     repo and the suite's credentials). Inside it, `cfg/` is 711 and `out/` is 733: the image's
#     non-root user, whatever its uid, can open the config by name and write its coverage data, and
#     nothing more. Measured: the container reaches a bind mount without the host path above it, and
#     a narrower `out/` (711, 730, 755) fails coverage's write. v1 made the whole directory 777.
#   - COVERAGE with coverage.py itself, not pytest-cov: `coverage run --branch` under the sysmon
#     core (fast branch coverage on Python 3.14), data in /tmp, then `coverage report` judged by
#     `luxlint --coverage-ratchet` against `[test].coverage_min` (off until you set a floor; it
#     only ratchets up). `coverage` belongs in the dev dependency group.
#   - BOTH EXIT CODES reach make: pytest's and the ratchet's. No pipe carries either. The ratchet
#     reads coverage's own report file, never the suite's output (a printed `TOTAL … 100%` or
#     pytest's `[100%]` would otherwise pass for a measurement), and a report that measured nothing
#     fails.
#   - Each run's project is named from its own `mktemp -d` token, and refuses to run without one (a
#     PID repeats across containers and CI runners), and everything mounts the makefile's directory ($(CURDIR)), so `make -C` runs the
#     right suite.

# Setting: the compose command, and the profile holding the test services --------------------
TEST_COMPOSE ?= docker compose
TEST_PROFILE ?= test
# Setting: the env file compose interpolates the file with (it reads EVERY service, so an app
# service's `${TAG:?}` needs a value even when only the test services start) -----------------
TEST_ENV_FILE ?= $(firstword $(wildcard .env.test .env.dev .env.example))
# Setting: the backing services the suite needs (each with a healthcheck); empty: none -----------
TEST_SERVICES ?= db-test
# Setting: the suite's environment: the test services' URLs, by service name on the test network -
TEST_ENV ?= -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@db-test:5432/postgres
# Setting: where the suite runs from (a monorepo's apps/backend), and every package coverage
# measures, comma-separated (`app,collector`): it replaces any [tool.coverage.run] source ---------
TEST_WORKDIR ?= .
TEST_COV ?= app
# Setting: NONE. For a one-off run only, on the command line: `make test PYTEST_ARGS='-k orders'`.
# A committed value narrows THE suite for everyone (`repo.test_block_wired` reds one). The ratchet
# still judges the whole floor, so a narrowed run measures only what it touched and fails it: a
# quick subset target of your own runs pytest without the ratchet, never through `make test` ----
PYTEST_ARGS ?=

# One-off pytest arguments reach the container through the environment, never spliced into a quoted
# command line (`-k 'a or b'` would otherwise split it).
export PYTEST_ARGS

test: test-build ## THE suite: test image, an isolated stack of real services, coverage ratchet
	@set -u; \
	D=$$(mktemp -d); tok=$$(basename "$$D" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9'); \
	[ $${#tok} -ge 8 ] || { echo "REFUSING: could not make a unique name for this run"; rm -rf "$$D"; exit 2; }; \
	run="t$$(printf '%s' '$(notdir $(CURDIR))' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')-test-$$tok"; \
	dc="$(TEST_COMPOSE) -p $$run $(if $(TEST_ENV_FILE),--env-file $(TEST_ENV_FILE)) --profile $(TEST_PROFILE)"; \
	trap '$$dc down --volumes --remove-orphans >/dev/null 2>&1; rm -rf "$$D"' EXIT INT TERM; \
	mkdir "$$D/cfg" "$$D/out" && chmod 711 "$$D/cfg" && chmod 733 "$$D/out" || exit 2; \
	net=; if [ -n "$(TEST_SERVICES)" ]; then \
	  $$dc up -d --wait --wait-timeout 120 $(TEST_SERVICES) \
	    || { echo "FAIL  the test services did not become healthy: $(TEST_SERVICES)"; exit 1; }; \
	  cid=$$($$dc ps -q $(firstword $(TEST_SERVICES))); net=; \
	  for n in $$(docker inspect -f '{{range $$k, $$v := .NetworkSettings.Networks}}{{$$k}} {{end}}' $$cid); do \
	    [ "$$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' $$n)" = "$$run" ] && { net=$$n; break; }; done; \
	  [ -n "$$net" ] || { echo "FAIL  $(firstword $(TEST_SERVICES)) joined no network of this run's own project ($$run)"; exit 1; }; \
	  net="--network $$net"; fi; \
	docker run --rm -v $(CURDIR):/repo $(LUXLINT) --emit-config pytest > "$$D/cfg/pytest.ini" || exit 2; \
	chmod 644 "$$D/cfg/pytest.ini"; \
	docker run --rm --init $$net $(TEST_ENV) -e PYTEST_ADDOPTS="$${PYTEST_ARGS:-}" \
	  -e COVERAGE_CORE=sysmon -e COVERAGE_FILE=/out/.coverage -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR):/repo:ro -v "$$D/cfg":/cfg:ro -v "$$D/out":/out -w /repo/$(TEST_WORKDIR) $(TEST_IMAGE) \
	  sh -c 'python -m coverage run --branch --source=$(TEST_COV) -m pytest -c /cfg/pytest.ini --rootdir=. -p no:cacheprovider; s=$$?; python -m coverage report --show-missing > /out/coverage.txt; echo $$? > /out/coverage.rc; cat /out/coverage.txt; exit $$s'; \
	rc=$$?; \
	[ "$$rc" = 0 ] || { echo "FAIL  the suite failed (exit $$rc)"; exit 1; }; \
	[ "$$(cat "$$D/out/coverage.rc" 2>/dev/null)" = 0 ] || { echo "FAIL  coverage measured nothing (coverage report: $$(tail -n 1 "$$D/out/coverage.txt" 2>/dev/null)): check TEST_COV names the package the suite imports"; exit 1; }; \
	docker run --rm -i -v $(CURDIR):/repo $(LUXLINT) --coverage-ratchet < "$$D/out/coverage.txt" > "$$D/ratchet.txt"; crc=$$?; \
	sed -n '/coverage ratchet/,$$p' "$$D/ratchet.txt"; \
	[ "$$crc" = 0 ] || { echo "FAIL  the coverage ratchet failed (its verdict is above): add tests, never lower the floor"; exit 1; }

# Local build tag — the e2e gate needs no registry.
E2E_IMAGE := sense-collector:e2e
test-e2e: harness-build ## Hardware-free end-to-end test: fake Sense endpoint -> collector -> InfluxDB
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
	docker run --rm -e LUXARCH_STATUS_WRITE=1 -v $(PWD):/repo $(LUXARCH_IMAGE) --json > $$t/lux.json || true; $(STAMP) $$t/lux.json .luxarch-status.json; \
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

# Public repo: the scan needs the private-registry luxlint/gitleaks images, so the registry legs
# skip gracefully when LUXARCH_REGISTRY is unset (the identity pass needs only git and always runs).
LUXLINT = $(LUXLINT_IMAGE)

# luxarch:gitleaks asset v14 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit gitleaks`.
# ── The privacy gate: EVERY surface a commit publishes ──────────────────────────────────────────
# Emitted by `luxarch --emit gitleaks`. Drop in verbatim.
#
# `gitleaks` scans DIFF CONTENT. A commit's author/committer address lives in the commit object
# HEADER and never appears in a patch, so no content rule can ever match it — it is a surface the
# scanner does not read. A repo reported `no leaks found` over 963 commits while 29 of them carried a
# personal address in both the author and committer fields, and it would have reported exactly the
# same thing after the scrub: identical output, opposite truth. Measured across the fleet, EIGHT
# repos carry a personal address in history and two of them are PUBLIC.
#
# v13: the commit MESSAGE is a third surface, and through v12 nothing read it. It is neither a patch
# nor the header, so a message naming an org-denylisted host passed the staged scan, the full-history
# scan and the push hook: seven commits in one public repo, found only when a file repeated the name.
# Every commit and annotated-tag message in history is now scanned with the same config as file
# content, and `gitleaks-commit-msg` scans the message being written (run by hooks/commit-msg).

# Commit identities this repo accepts. The fleet account's `users.noreply.github.com` address, plus
# GitHub's own web-UI committer. Widen ONLY for a real outside contributor, with a comment saying who.
# NOT for the org account's real address: a role mailbox in commit metadata is published with every
# clone exactly like a personal one (six fleet repos carried it, one PUBLIC). Its
# omission here is the policy, not an oversight: the answer is the scrub printed below, and the
# repo's agent performs it once the OWNER approves the force-push.
# Anchored on the CLOSING BRACKET, because the compared line is `Name <email>` — not a bare
# address. The first cut allowed `^noreply@github.com$$`, which can NEVER match a
# `Name <email>` line, so the GitHub web-UI identity was silently DENIED and the canonical
# recipe would have refused on any repo carrying a web-UI commit. Measured across the fleet: it
# denied 4 of 6 distinct identity lines instead of the 3 real offenders.
# It was missed because the only repo it was tested on has no web-UI commits, so the broken
# branch never ran. The bracket also closes a substring hole: unanchored,
# `<x@users.noreply.github.com.attacker.test>` would have been allowed.
# v11: the noreply address is `<local@users.noreply.github.com>`, and the local part has no `@`. v10's
# `<[^>]*users…` admitted `<dev.real@gmail.com.users.noreply.github.com>`, a real address in the clear.
# v12: the fleet ACCOUNT, not any noreply address. v11 accepted `<anyone@users.noreply.github.com>`, so a
# stranger's (or a second account's) noreply committer passed. Measured: the fleet's whole history holds
# exactly two noreply identities, the fleet account and GitHub's web-UI committer.
GIT_IDENTITY_OK ?= <214140984\+luxardolabs@users\.noreply\.github\.com>$$|<noreply@github\.com>$$

# The secret scanner, PINNED and MIRRORED in the fleet registry. The fleet bans a moving tag
# everywhere it can see one, and this used to ship `ghcr.io/gitleaks/gitleaks:latest` inside the asset every
# repo adopts verbatim: the privacy gate could not run with ghcr unreachable or the local copy pruned, and
# nothing recorded which scanner said "no leaks found". New detection rules still arrive, through the fleet's
# own mechanism: luxarch bumps this pin in a release, and `repo.emitted_assets_current` tells you to re-emit.
# v5: the HOST is never written here. v4 inlined the private registry, so dropping
# this asset in "verbatim" put the host into a committed Makefile, and on a public repo the fleet's
# own gitleaks disclosure tier refused the commit. The mirror lives beside the guards, so the ref is
# derived from wherever this repo already pulls luxlint (`$(LUXLINT)`, which the scan below needs
# anyway). It works whichever variable holds your guard registry (REGISTRY, LUXARCH_REGISTRY, …).
# Recursive `=` so it resolves at use, whatever order LUXLINT is defined in.
# v9: PINNED BY DIGEST, and buildable off-network. The digest is the scanner's identity; the registry is
# only where it is fetched from. Beside a registry-qualified `$(LUXLINT)` it pulls the fleet mirror; with
# a local guard build (`luxlint:local`, on a machine with no access to the fleet registry, such as an
# airgapped laptop) it pulls the public image. v8 derived `./gitleaks:…` there, an unpullable reference, so the
# privacy gate could not run at all. The mirror and the public image share the digest, so both
# resolve to the same bits, and a tampered or re-tagged copy fails the pull instead of scanning.
GITLEAKS_IMAGE = $(if $(findstring /,$(LUXLINT)),$(dir $(LUXLINT)),zricethezav/)gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f

# v13: `/repo` is `$(CURDIR)`, this makefile's directory. v1-v12 mounted `$(PWD)`, the CALLER's shell
# directory, so `make -C <repo> gitleaks` run from another repo scanned THAT repo's history with THAT
# repo's config while the identity pass read this one: two repos' answers, reported as this one's.
gitleaks: ## secret scan over FULL HISTORY + the commit-identity pass (the hooks cover commit/push)
	@set -e; C=$$(mktemp); trap 'rm -f "$$C"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	docker run --rm -v "$(CURDIR)":/repo -v "$$C":/gl.toml:ro -w /repo \
	  $(GITLEAKS_IMAGE) git /repo -c /gl.toml --redact -v --ignore-gitleaks-allow
	@# v12: what `gitleaks git` never reads. It scans git's PATCHES, and git prints no patch for a file it
	@# treats as binary: a NUL byte, UTF-16 (a `Localizable.strings`), or a `binary` / `-diff` attribute.
	@# A token in any of them reached the remote with "no leaks found". So every path git ever showed as
	@# binary is re-read as text, NULs stripped, and scanned by path (the config's path allowlists hold).
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	git -c core.quotePath=false log --branches --tags HEAD --format= -p --no-ext-diff --no-textconv \
	  > "$$D/patches"; \
	sed -n 's|^Binary files .* and b/\(.*\) differ$$|\1|p' "$$D/patches" | sort -u > "$$D/binary"; \
	if [ -s "$$D/binary" ]; then \
	  mkdir "$$D/t" "$$D/none"; \
	  while IFS= read -r f; do \
	    mkdir -p "$$D/t/$$(dirname "$$f")"; \
	    git -c core.quotePath=false log --branches --tags HEAD --format= -p --text --no-ext-diff \
	      --no-textconv -- "$$f" > "$$D/p"; \
	    grep -a '^+' "$$D/p" | tr -d '\000' > "$$D/t/$$f"; \
	  done < "$$D/binary"; \
	  docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	    $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	    --gitleaks-ignore-path /none; \
	fi
	@# v13: every commit and annotated-tag MESSAGE, scanned as a file with the same config (credential
	@# rules and the denylist). One `git log` and one `for-each-ref`, split on a header line carrying a
	@# per-run nonce, so a message cannot forge a header and steer its own lines out of the scan.
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	N=$$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n'); [ $${#N} -eq 32 ]; \
	mkdir -p "$$D/t/commit-message" "$$D/t/tag-message" "$$D/none"; \
	git log --branches --tags HEAD --format="$$N commit-message/%H%n%B" > "$$D/raw"; \
	git for-each-ref refs/tags --format="$$N tag-message/%(objectname)%0a%(contents)" >> "$$D/raw"; \
	tr -d '\000' < "$$D/raw" > "$$D/msgs"; \
	LC_ALL=C awk -v n="$$N" -v d="$$D/t" \
	  '$$1 == n && NF == 2 { if (f != "") close(f); f = d "/" $$2; printf "" > f; next } \
	   f != "" { print > f }' "$$D/msgs"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none || { \
	  echo "REFUSING: a commit or tag MESSAGE carries a finding (the File above names it:"; \
	  echo "  commit-message/<sha> or tag-message/<object>). A message is published with its commit"; \
	  echo "  forever. Reword it (git rebase -i, or git filter-repo --message-callback for older"; \
	  echo "  history), re-run, and ask the OWNER to approve the force-push. Never force-push unapproved."; \
	  exit 1; }
	@# v14: what a push publishes besides content and messages: every file PATH in history and every
	@# branch and tag NAME. A path is not a patch line, so `gitleaks git` never read one, and a file or
	@# a branch named for an internal host passed every gate into a public remote.
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir -p "$$D/t" "$$D/none"; \
	git -c core.quotePath=false log --branches --tags HEAD --format= --name-only | sort -u > "$$D/t/paths"; \
	git for-each-ref --format='%(refname:short)' refs/heads refs/tags > "$$D/t/ref-names"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none || { \
	  echo "REFUSING: a file PATH or a branch/tag NAME carries a finding (File: paths or ref-names above)."; \
	  echo "  A name is published with the history it is in. Rename it (git mv; git branch -m; for older"; \
	  echo "  history git filter-repo --path-rename), re-run, and ask the OWNER to approve the force-push."; \
	  exit 1; }
	@# The identity pass — the half gitleaks structurally cannot do. Cheap: one `git log`.
	@# Walks what THIS repo publishes (branches, tags, HEAD), NOT `--all`: a remote-tracking ref caches the
	@# remote's state, which during a scrub is by definition the un-rewritten history you are about to
	@# force-push over — `--all` refused the verified fix, and any `git fetch` re-armed it.
	@bad=$$(git log --branches --tags HEAD --pretty='%an <%ae>%n%cn <%ce>' 2>/dev/null | sort -u \
	  | grep -vE '$(GIT_IDENTITY_OK)' || true); \
	if [ -n "$$bad" ]; then \
	  echo "REFUSING: a non-fleet identity appears in commit METADATA (author/committer):"; \
	  echo "$$bad" | sed 's/^/    /'; \
	  echo "gitleaks cannot see this — it scans diffs, not commit headers, so it reported no leaks."; \
	  echo "An address here is attached to every affected commit forever, not to one line of one file."; \
	  echo "Scrub per FLEET-ONBOARDING-STANDARD §2: mirror backup -> git filter-repo -> re-verify with"; \
	  echo "  git log --branches --tags HEAD --pretty='%an <%ae>%n%cn <%ce>' | sort -u"; \
	  echo "-> ask the OWNER to approve the force-push, then do it yourself. Never force-push unapproved."; \
	  exit 1; \
	fi

# v6: the STAGED scan the commit hook calls (`hooks/pre-commit` → `make gitleaks-staged`) is part of the
# asset now. v5 shipped only the full-history half, so 9 of 10 adopting repos hand-wrote this target
# and the tenth had none, leaving its pre-commit hook pointing at a missing recipe. If your Makefile
# carries its own `gitleaks-staged`, delete it when you re-emit: this one replaces it.
# v7: `-w /repo` is LOAD-BEARING. Without it git runs outside the repo, falls back to `git diff
# --no-index`, rejects `--staged`, and gitleaks EXITS 0: v6 let a staged secret through while printing
# a git error (measured on a planted GitHub token: v6 exit 0, v7 "leaks found: 1" exit 1).
# v8: the denylist goes to a PER-RUN `mktemp` file, removed on exit. v7 wrote a fixed
# `/tmp/gl.toml` that outlived the run: on a host where commit and push run as different users, the
# next user's redirect was refused (`fs.protected_regular=1`, the Fedora default, blocks O_CREAT on
# another user's file in sticky /tmp even for root), so the privacy gate failed every commit or push
# after a user switch (2 of 2 measured). Two repos scanning at once also shared one file, so one could
# scan with the other's carve-outs. The full-history scan now also passes `-w /repo`, like the staged one.
# v12: the staged bytes are read on the HOST and scanned as files, by path. v11 ran `protect --staged`
# inside the container, which read `.git/index`, not the temporary index git hands the hook in
# `$$GIT_INDEX_FILE`: `git commit -a` and `git commit <path>` were never scanned. It also read git's
# patches, which skip binary, NUL, UTF-16 and `-diff` files, and it honoured a `# gitleaks:allow` on
# the secret's own line and a committed `.gitleaksignore`, so a commit's author could waive their own
# leak. New content gets no waiver: `.gitleaksignore` stays a ledger for the full-history scan only.
gitleaks-staged: ## secret scan of the STAGED changes (run by hooks/pre-commit)
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir "$$D/t" "$$D/none"; \
	git -c core.quotePath=false diff --cached --name-only --diff-filter=ACMR -z > "$$D/names"; \
	tr '\000' '\n' < "$$D/names" | while IFS= read -r f; do \
	  [ -n "$$f" ] || continue; \
	  mkdir -p "$$D/t/$$(dirname "$$f")"; \
	  git -c core.quotePath=false diff --cached --text --no-ext-diff --no-textconv -U0 -- "$$f" \
	    > "$$D/p"; \
	  grep -a '^+' "$$D/p" | tr -d '\000' > "$$D/t/$$f"; \
	done; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none

# v13: the message being WRITTEN, run by hooks/commit-msg (pre-commit cannot see it: git writes the
# message after that hook runs). The hook passes the file in `GITLEAKS_COMMIT_MSG`. The WHOLE file is
# scanned, comment lines included: `--cleanup=verbatim` publishes them, and a cut at the scissors line
# would let a verbatim message hide everything below one. No file to read is a refusal, never a pass.
gitleaks-commit-msg: ## secret + denylist scan of the commit MESSAGE being written (run by hooks/commit-msg)
	@set -e; M="$${GITLEAKS_COMMIT_MSG:-}"; \
	if [ -z "$$M" ] || [ ! -f "$$M" ] || [ ! -r "$$M" ]; then \
	  echo "REFUSING: no commit message to scan (GITLEAKS_COMMIT_MSG='$$M'). hooks/commit-msg" >&2; \
	  echo "  passes the file git hands it; re-emit the hooks with luxlint --emit-hooks." >&2; \
	  exit 1; \
	fi; \
	C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir -p "$$D/t/commit-message" "$$D/none"; \
	tr -d '\000' < "$$M" > "$$D/t/commit-message/COMMIT_EDITMSG"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none || { \
	  echo "REFUSING: this commit's MESSAGE carries a finding. A message is published with the" >&2; \
	  echo "  commit forever: reword it and commit again. Never --no-verify." >&2; \
	  exit 1; }

hooks: ## Install the committed git hooks (hooks/) — gitleaks on every commit + push (luxlint --emit-hooks)
	@if $(NO_REGISTRY); then $(SKIP_MSG); exit 0; fi; \
	$(GUARD_RUN) $(LUXLINT_IMAGE) --emit-hooks | sh

##@ Utilities

clean: ## Clean python/test caches
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage htmlcov/

clean-all: clean docker-clean ## Clean caches + local docker image tags
