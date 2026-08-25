.PHONY: setup format lint lint-fix unit integration build compose-check check clean

PY ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python

setup:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

format:
	$(VENV)/bin/ruff format --check .

lint:
	$(VENV)/bin/ruff check .

lint-fix:
	$(VENV)/bin/ruff check --fix .

unit:
	$(PYTHON) -m pytest tests/unit -q

# Requires Docker (starts a disposable test PostgreSQL).
integration:
	$(PYTHON) -m pytest tests/integration tests/api -q

build:
	docker compose build

compose-check:
	docker compose config -q

check: lint unit integration compose-check build
	@echo "ALL CHECKS PASSED"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .coverage
