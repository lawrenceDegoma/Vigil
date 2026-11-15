# Vigil developer commands. Run `make help`.
EDGE := services/edge
UV   := uv --directory $(EDGE)

.PHONY: help setup test test-fast lint format typecheck check demo analyze evaluate clean

help:
	@echo "setup      install the edge dev environment (uv, Python 3.11)"
	@echo "test       run the full edge test suite"
	@echo "test-fast  skip slow end-to-end tests"
	@echo "lint       ruff lint + format check"
	@echo "format     apply ruff formatting"
	@echo "typecheck  mypy (strict)"
	@echo "check      lint + typecheck + test"
	@echo "demo       generate a synthetic scenario and analyze it (writes runs/demo)"
	@echo "analyze    VIDEO=path [ARGS='--plot --overlay'] analyze a video file"
	@echo "evaluate   run the synthetic evaluation set"

setup:
	$(UV) sync --python 3.11 --extra plot

test:
	$(UV) run pytest

test-fast:
	$(UV) run pytest -m "not slow"

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

typecheck:
	$(UV) run mypy

check: lint typecheck test

demo:
	./scripts/demo.sh

analyze:
	@test -n "$(VIDEO)" || (echo "usage: make analyze VIDEO=path/to/clip.mp4 [ARGS=...]" && exit 2)
	uv run --project $(EDGE) vigil analyze "$(VIDEO)" $(ARGS)

evaluate:
	uv run --project $(EDGE) vigil evaluate --synthetic

clean:
	rm -rf runs $(EDGE)/.pytest_cache $(EDGE)/.mypy_cache $(EDGE)/.ruff_cache
