# Shortcuts for working on this repository from your own computer. Run `make` to list them.
SHELL := /bin/bash
PYTHON ?= python3
VENV := venv

.PHONY: all help init check-python lint test clean

all: help

# A venv/ with pre-commit, and its pre-commit and commit-msg hooks, and a .env from .env.example if
# there is none. --clear starts from an empty venv, which also repairs one whose Python was moved
# or upgraded.
init: check-python
	@[ -f .env ] || { cp .env.example .env && echo "Created .env from .env.example."; }
	$(PYTHON) -m venv --clear $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -r requirements.txt
	$(VENV)/bin/pre-commit install
	$(VENV)/bin/pre-commit install-hooks
	@echo ""
	@echo "Ready. The checks now run before each commit, and make lint runs them on every file."

check-python:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo >&2 "This repository needs $(PYTHON): install it, or run make init PYTHON=/path/to/python3"; exit 1; }

# The pre-commit hooks on every file: the standard, as in CI (test.yml).
lint:
	@[ -x $(VENV)/bin/pre-commit ] || { echo "Run make init first."; exit 1; }
	$(VENV)/bin/pre-commit run --all-files

# The scripts' tests. -t scripts: the scripts import each other by name, as on the runner.
test: check-python
	$(PYTHON) -m unittest discover -s scripts/tests -t scripts

clean:
	rm -rf $(VENV)
	find . -name __pycache__ -type d -prune -not -path "./$(VENV)/*" -exec rm -rf {} +

help:
	@echo "===================================================================="
	@echo "init           - Create venv/ with pre-commit, install the git hooks, and create .env"
	@echo "lint           - Run the pre-commit hooks (actionlint among them) on every file"
	@echo "test           - Run the scripts' tests (scripts/tests)"
	@echo "clean          - Remove venv/ and __pycache__ folders"
	@echo "===================================================================="
