.PHONY: test lint mutants

test:
	pytest -q

lint:
	ruff check .
	mypy --strict wxmm
	lint-imports

mutants:
	python tests/mutants/run.py
