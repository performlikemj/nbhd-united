.PHONY: setup migrate run test lint compile-deps sync-deps docker-up docker-down celery celery-beat superuser tenants health

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

compile-deps:
	pip-compile requirements.in

sync-deps:
	pip-sync requirements.txt

docker-up:
	docker compose up -d

docker-down:
	docker compose down

celery:
	celery -A config worker -l info

celery-beat:
	celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

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
