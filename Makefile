.DEFAULT_GOAL := setup
.PHONY: setup unit test
export TMPDIR := $(CURDIR)/tmp

setup:
	mkdir -p tmp
	uv sync --frozen

unit: setup
	uv run pytest -q --basetemp=tmp/pytest

test: unit
