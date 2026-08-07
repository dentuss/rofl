# rofl — short commands for the things done often.
# `make` on its own prints this list.

PY  := ./.venv/bin/python
PIP := ./.venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help venv test setup pull ticks live health watch status report backup prune

help:  ## show this help
	@echo "rofl — make targets"
	@echo
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  first time:  make venv && make setup && make pull"

venv:  ## create .venv and install requirements
	python3 -m venv .venv
	$(PIP) install -q -r requirements.txt
	@$(PY) -c "import pandas,sklearn,ccxt;print('deps ok', pandas.__version__)"

test:  ## run every test suite (plain-assert, no pytest)
	@fail=0; for t in test_*.py; do \
	  printf "%-30s " "$$t"; \
	  if PYTHONIOENCODING=utf-8 $(PY) $$t >/tmp/rofl_test.$$$$ 2>&1; then echo PASS; \
	  else echo FAIL; tail -5 /tmp/rofl_test.$$$$ | sed 's/^/    /'; fail=1; fi; \
	  rm -f /tmp/rofl_test.$$$$; done; \
	  [ $$fail -eq 0 ] && echo "ALL GREEN" || (echo "SUITE RED"; exit 1)

# ---------------------------------------------------------------- data pipeline
setup:  ## one-time: enter the two Oracle box IPs, verify SSH
	@deploy/pull-data.sh setup

pull:  ## pull BOTH boxes into ./data, then print the health report
	@deploy/pull-data.sh pull

ticks:  ## pull the collector box only
	@deploy/pull-data.sh ticks

live:  ## pull the trading box only
	@deploy/pull-data.sh live

health:  ## health report on whatever data is already local
	@deploy/pull-data.sh health

watch:  ## pull on a loop (make watch SECS=120)
	@deploy/pull-data.sh watch $(or $(SECS),300)

status:  ## show configured hosts, reachability and local sizes
	@deploy/pull-data.sh status

prune:  ## drop local *.csv where a verified *.csv.gz exists (frees ~75MB/day)
	@n=0; freed=0; \
	for gz in data/ticks/*/*.csv.gz; do \
	  [ -e "$$gz" ] || continue; plain="$${gz%.gz}"; \
	  if [ -f "$$plain" ] && gzip -t "$$gz" 2>/dev/null; then \
	    freed=$$((freed + $$(stat -c %s "$$plain"))); rm -f "$$plain"; n=$$((n+1)); \
	  fi; done; \
	echo "pruned $$n stale .csv ($$((freed/1024/1024)) MB freed)"

backup:  ## tar the local tick tree (irreplaceable — no backfill exists)
	@mkdir -p backups && tar czf backups/ticks-$$(date -u +%F).tgz -C data ticks \
	  && echo "wrote backups/ticks-$$(date -u +%F).tgz ($$(du -h backups/ticks-$$(date -u +%F).tgz | cut -f1))"
	@ls -1t backups/ticks-*.tgz 2>/dev/null | tail -n +8 | xargs -r rm -f
	@echo "keeping $$(ls -1 backups/ticks-*.tgz 2>/dev/null | wc -l) backup(s)"

# ---------------------------------------------------------------- research
report:  ## re-run the canonical deploy report (the book's honest numbers)
	@PYTHONIOENCODING=utf-8 $(PY) research/deploy_report.py
