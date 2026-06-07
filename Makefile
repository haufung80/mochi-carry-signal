# mochi-carry-signal — dev tasks.
#
# The funding-arb HTTP contract is OWNED by mochi-position-manager (the provider).
# This repo vendors a PINNED copy at tests/contract/openapi-funding-arb.yaml and
# tests/test_pm_contract.py asserts our PMClient still conforms to it. When the
# provider changes the contract (edit its schemas + `make openapi` THERE), re-sync
# the pinned copy here with `make vendor-contract`, then update pm_client to match.

PM_REPO ?= ../mochi-position-manager
CONTRACT_SRC := $(PM_REPO)/docs/openapi-funding-arb.yaml
CONTRACT_DST := tests/contract/openapi-funding-arb.yaml

.PHONY: install test vendor-contract

install:
	pip install -e ".[dev]"

test:
	python -m pytest

# Re-vendor the provider's funding-arb OpenAPI as the pinned contract reference.
# Override the provider location with: make vendor-contract PM_REPO=/path/to/mochi-position-manager
vendor-contract:
	@test -f "$(CONTRACT_SRC)" || { echo "provider spec not found: $(CONTRACT_SRC) (set PM_REPO=...)"; exit 1; }
	cp "$(CONTRACT_SRC)" "$(CONTRACT_DST)"
	@echo "vendored $(CONTRACT_SRC) -> $(CONTRACT_DST)"
	@echo "now run: python -m pytest tests/test_pm_contract.py  (and update pm_client.py if it drifted)"
