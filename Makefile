.PHONY: test lint mutants

PYTHON ?= python3

test:
	$(PYTHON) -m pytest -q

lint:
	ruff check .
	mypy --strict wxmm
	lint-imports

mutants:
	$(PYTHON) tests/mutants/run.py
