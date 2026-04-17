.PHONY: setup migrate run test lint harness compile-deps sync-deps docker-up docker-down superuser tenants health \
	provision deprovision \
	canary canary-build canary-deploy canary-logs canary-health canary-rollback canary-prune

# Canary defaults — override on the command line, e.g. `make canary CANARY_CONTAINER=oc-...`
CANARY_CONTAINER ?= oc-148ccf1c-ef13-47f8-a
CANARY_REGISTRY  ?= nbhdunited
CANARY_REPO      ?= nbhd-openclaw
CANARY_RG        ?= rg-nbhd-prod
CANARY_TAG       ?= canary-$(shell git rev-parse --short HEAD)
ADMIN_HEALTH_URL ?= https://nbhd-django-westus2.victoriousocean-5cdd2683.westus2.azurecontainerapps.io/api/v1/cron/admin-health/

setup:
	python -m venv .venv
	.venv/bin/pip install pip-tools
	.venv/bin/pip-compile requirements.in
	.venv/bin/pip-sync requirements.txt

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

compile-deps:
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
