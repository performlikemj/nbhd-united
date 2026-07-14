.PHONY: setup migrate run test lint harness integrate-gate compile-deps sync-deps docker-up docker-down superuser tenants health \
	provision deprovision \
	canary canary-build canary-deploy canary-logs canary-health canary-rollback canary-prune

# Canary defaults — override on the command line, e.g. `make canary CANARY_CONTAINER=oc-...`
CANARY_CONTAINER ?= oc-148ccf1c-ef13-47f8-a
CANARY_REGISTRY  ?= nbhdunited
CANARY_REPO      ?= nbhd-openclaw
CANARY_RG        ?= rg-nbhd-prod
CANARY_TAG       ?= canary-$(shell git rev-parse --short HEAD)
ADMIN_HEALTH_URL ?= https://nbhd-django-westus2.victoriousocean-5cdd2683.westus2.azurecontainerapps.io/api/v1/cron/admin-health/

# Builds .venv at CI parity from origin/main's requirements.txt. It deliberately does
# NOT run pip-compile: on macOS that silently DROPS ~37 Linux-only CUDA/triton pins from
# requirements.txt, and the loss stays invisible until the PII container fails to deploy.
# This target used to do precisely that, guarded by nothing but one sentence in a
# reference doc — and `make setup` is the very first thing anyone reaches for when told
# their venv has drifted, which is a message the parity hook now actively emits. A warning
# whose obvious remedy is a landmine is worse than no warning.
# Deliberate regeneration still lives in `compile-deps` (hook-blocked on macOS — run it
# inside the Linux container).
setup:
	./scripts/rebuild_venv.sh

migrate:
	python manage.py migrate

run:
	python manage.py runserver 0.0.0.0:8000

test:
	python manage.py test apps/

lint:
	ruff check .

harness:
	./scripts/harness-check --project . --scope staged

# ---------------------------------------------------------------------------
# Integration train gate — the single command `/integrate` runs to prove a
# branch combination locally before it lands. Mirrors ci-cd.yml's frontend-test
# and backend-test gates at CI strictness: a gate weaker than CI stamps a lie.
# `ruff format --check` and `makemigrations --check` are the separate stricter
# gates a plain `make lint`/`make test` misses; `npm ci` is CI's frozen-lockfile
# install (lockfile drift has broken CI before). Bare `ruff`/`python` like the
# lint/test targets — the train runs this from a throwaway worktree with no
# .venv, so it relies on the activated venv on PATH, not a relative .venv/ path.
# One gate per recipe line — make aborts on the first non-zero, so it fails fast.
# The test step gets its own DB name (DJANGO_TEST_DB_NAME, wired in base.py) plus
# --noinput because ~25 worktrees share one Postgres: a shared default test DB got
# destroyed mid-run by a concurrent session, and a leftover one blocked on the
# interactive recreate prompt. Its own name + auto-recreate makes the train hermetic.
#
# Deliberately NOT mirrored: the backend secret-scan and config-validator +
# security-audit shell steps, and the whole openclaw-config-smoke job (plugin
# packaging guard, doctor smoke, node --test suites). Those are heavier CI-side
# guards that still run in CI on the PR — a green stamp here covers lint, format,
# migration-drift, the backend suite, and the frontend lint+build only.
# See docs/agents/workflow.md "Parallel work & deploy serialization".
integrate-gate:
	ruff check .
	ruff format --check .
	python manage.py makemigrations --check --dry-run
	DJANGO_TEST_DB_NAME=test_nbhd_united_train python manage.py test apps/ --noinput
	cd frontend && npm ci && npm run lint && npm run build

# Refuses off Linux, IN THE RECIPE — not just in the Claude hook. A string-matching hook
# guards only the doorway: it lives inside Claude sessions, so a human typing this in a plain
# terminal has none, a worktree branched before this PR carries the old hook, and every make
# spelling (`-j2`, `-C .`, trailing targets) is another string to chase. The damage is silent
# and deferred — ~37 Linux CUDA/triton pins vanish from requirements.txt and nothing fails
# until the PII container deploys — so the refusal belongs where the damage is done.
# git_guard.sh stays as a backstop for raw `pip-compile` invocations, which is all a string
# match can honestly be.
compile-deps:
	@[ "$$(uname)" = "Linux" ] || { \
	  echo "REFUSING: pip-compile on macOS re-resolves for THIS platform and silently drops"; \
	  echo "~37 Linux-only CUDA/triton pins from requirements.txt. Nothing fails until the"; \
	  echo "PII container deploys. Run this inside the Linux container, or hand-edit"; \
	  echo "requirements.txt. To rebuild your virtualenv you want: make setup"; \
	  exit 1; }
	pip-compile requirements.in

sync-deps:
	pip-sync requirements.txt

docker-up:
	docker compose up -d

docker-down:
	docker compose down

superuser:
	python manage.py createsuperuser

# Management commands
tenants:
	python manage.py list_tenants

health:
	python manage.py check_health

provision:
	@test -n "$(TENANT_ID)" || (echo "Usage: make provision TENANT_ID=<uuid>" && exit 1)
	python manage.py provision_tenant $(TENANT_ID)

deprovision:
	@test -n "$(TENANT_ID)" || (echo "Usage: make deprovision TENANT_ID=<uuid>" && exit 1)
	python manage.py deprovision_tenant $(TENANT_ID)

# ---------------------------------------------------------------------------
# Canary — single-tenant pre-merge OpenClaw validation.
# See docs/runbooks/canary.md for the full procedure.
#
# Quick path:   make canary                      # build + deploy current HEAD
# Health poll:  make canary-health
# Rollback:     make canary-rollback PREV_TAG=<sha>
# ---------------------------------------------------------------------------

canary: canary-build canary-deploy
	@echo ""
	@echo "Canary deployed: $(CANARY_TAG) -> $(CANARY_CONTAINER)"
	@echo "Next: make canary-logs   (watch startup)"
	@echo "      make canary-health  (gate before merge)"

canary-build:
	@echo "Building $(CANARY_REGISTRY).azurecr.io/$(CANARY_REPO):$(CANARY_TAG)"
	az acr build \
		--registry $(CANARY_REGISTRY) \
		--image $(CANARY_REPO):$(CANARY_TAG) \
		--file Dockerfile.openclaw \
		.

canary-deploy:
	@test -n "$(CANARY_TAG)" || (echo "CANARY_TAG is empty (is HEAD a valid commit?)" && exit 1)
	python manage.py canary_tenant_image \
		--container $(CANARY_CONTAINER) \
		--tag $(CANARY_TAG) \
		--repository $(CANARY_REPO)

canary-logs:
	az containerapp logs show \
		--name $(CANARY_CONTAINER) \
		--resource-group $(CANARY_RG) \
		--tail 200 \
		--follow

canary-health:
	@test -n "$$DEPLOY_SECRET" || (echo "DEPLOY_SECRET env var is required" && exit 1)
	@curl -sf -H "X-Deploy-Secret: $$DEPLOY_SECRET" "$(ADMIN_HEALTH_URL)" \
		| jq '.tenants[] | select(.container_id == "$(CANARY_CONTAINER)")'

canary-rollback:
	@test -n "$(PREV_TAG)" || (echo "Usage: make canary-rollback PREV_TAG=<sha>" && exit 1)
	python manage.py canary_tenant_image \
		--container $(CANARY_CONTAINER) \
		--tag $(PREV_TAG) \
		--repository $(CANARY_REPO)

canary-prune:
	@echo "Canary tags in $(CANARY_REGISTRY)/$(CANARY_REPO) (newest first):"
	@az acr repository show-tags \
		--name $(CANARY_REGISTRY) \
		--repository $(CANARY_REPO) \
		--orderby time_desc \
		--query "[?starts_with(@, 'canary-')]" -o tsv
	@echo ""
	@echo "Delete with: az acr repository delete --name $(CANARY_REGISTRY) --image $(CANARY_REPO):<tag>"
