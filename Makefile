# QuantSwarm — common workflows. Everything runs offline (no API keys) using committed fixtures.
API ?= http://localhost:8000
RUN ?= $(shell ls -t data/manifests/T*.json 2>/dev/null | grep -v -e session -e solana | head -1)

.PHONY: setup up down reset fixture tournament session demo web test prove bench report crash-worker restart-worker solana-commit verify-solana

setup:            ## install Python + web dependencies
	uv sync
	cd apps/web && npm install --no-audit --no-fund

up:               ## start TimescaleDB, execution worker and API (resource-limited containers)
	docker compose up -d --build db worker api

down:
	docker compose down

reset:            ## wipe the database volume (all ledgers)
	docker compose down -v

fixture:          ## regenerate the committed synthetic dataset (deterministic, seed 42)
	uv run python scripts/make_fixture.py

tournament:       ## agents propose -> grid search on train -> rank on validation -> allocate -> test once
	uv run python -m quantswarm.agents.tournament --api $(API)

session:          ## replay the held-out period through API -> worker, with a crash at 50%
	uv run python -m quantswarm.agents.session --api $(API)

demo: up tournament session   ## full pipeline end to end

web:              ## trading terminal on http://localhost:3000
	cd apps/web && npm run dev

test:
	uv run pytest -q

prove:            ## run all tests and print the invariant evidence table
	uv run python scripts/prove.py

bench:            ## engine + DB + recovery benchmarks, then regenerate docs/PERFORMANCE.md
	uv run python benchmarks/run_benchmarks.py engine
	uv run python benchmarks/run_benchmarks.py db
	uv run python benchmarks/run_benchmarks.py recovery
	uv run python benchmarks/report.py

report:
	uv run python benchmarks/report.py

crash-worker:     ## SIGKILL the execution worker (commands keep queueing in Postgres)
	docker compose kill -s SIGKILL worker

restart-worker:   ## restart it: replays the hash-chained log, verifies the digest, resumes
	docker compose start worker
	@sleep 2; curl -s $(API)/v1/verify | python3 -m json.tool

solana-commit:    ## publish sha256(manifest) to Solana Devnet as an SPL Memo
	uv run python -m quantswarm.audit.solana commit $(RUN) --api $(API)

verify-solana:    ## recompute manifest hash and compare with the on-chain memo
	uv run python -m quantswarm.audit.solana verify $(RUN)
