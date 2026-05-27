.PHONY: install dev test lint format migrate

install:
	pip install -r requirements.txt -r requirements-dev.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80 -v

lint:
	flake8 app/ tests/
	mypy app/

format:
	black app/ tests/
	isort app/ tests/

migrate:
	alembic upgrade head
