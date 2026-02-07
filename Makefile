.PHONY: setup migrate run test lint compile-deps sync-deps docker-up docker-down

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
	python manage.py test

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
