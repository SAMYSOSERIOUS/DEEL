.PHONY: install check-sources all daily retrain analyse test test-all demo site

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"

check-sources:
	desave check-sources

all:            ## first full run: download, analyse, train, backfill, page
	desave all

daily:
	desave daily

retrain:
	desave download && desave retrain

analyse:
	desave analyse

site:
	desave site

test:           ## fast tests
	pytest -m "not slow"

test-all:       ## everything, incl. the planted-saving run (a few minutes)
	pytest

demo:           ## whole project offline on stand-in data
	desave demo --dir demo_run
