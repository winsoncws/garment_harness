# =====================================================================
# Every command works with NO GPU: append `offline` variants use the
# fake model. Run `make help` to see this list.
# =====================================================================
PY ?= python3

.DEFAULT_GOAL := help

help:                       ## show this list
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

doctor:                     ## check python, ollama, models, database
	$(PY) -m gmt doctor

init:                       ## create data/garment.db
	$(PY) -m gmt init

tasks:                      ## list tasks and how they route
	$(PY) -m gmt tasks -v

test:                       ## run the whole test suite (offline, ~1s)
	$(PY) tests/run_tests.py

demo:                       ## full offline walkthrough -- START HERE
	@rm -f data/garment.db data/garment.db-*
	$(PY) -m gmt --offline init
	$(PY) -m gmt --offline ingest
	$(PY) -m gmt --offline ask "which machine had the most defects?"
	$(PY) -m gmt --offline stats

ingest:                     ## ingest the seed notes using OLLAMA
	$(PY) -m gmt ingest

ask:                        ## make ask Q="which shift is worst?"
	$(PY) -m gmt ask "$(Q)"

bench:                      ## compare tier 1 vs tier 2 on the golden set
	$(PY) -m gmt bench --tiers 1,2

bench-offline:              ## same, with the fake model (proves the harness)
	$(PY) -m gmt --offline bench --tiers 1,2

stats:                      ## what is in the store, and how models performed
	$(PY) -m gmt stats

mcp:                        ## run the MCP server on stdio (for an agent)
	$(PY) -m gmt.mcp.server

clean:                      ## delete the database and caches
	rm -f data/garment.db data/garment.db-* /tmp/gmt_test.db*
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

.PHONY: help doctor init tasks test demo ingest ask bench bench-offline stats mcp clean
