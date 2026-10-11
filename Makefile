# QuantSwarm — common workflows. Everything runs offline (no API keys) using committed fixtures.
# Recipes avoid Unix-only tools so they also work with GNU make on Windows (cmd.exe or Git Bash).
API ?= http://localhost:8000
RUN ?= $(shell uv run python -c "import glob, os; f = [p for p in glob.glob('data/manifests/T*.json') if p.count('.') == 1]; print(max(f, key=os.path.getmtime).replace(os.sep, '/') if f else '')")

.PHONY: check-integrations setup up backend down reset fixture tournament session demo web test prove prove-docker bench report crash-worker restart-worker

setup: auth-init   ## install Python + web dependencies
	uv sync
	cd apps/web && npm ci --no-audit --no-fund

up: auth-init      ## start the persisted stack and local job manager on http://localhost:3000
	docker compose up -d --build db worker api manager web

backend: auth-init ## rebuild backend services in place, preserving the database volume
	docker compose up -d --build db worker api manager

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

web:              ## terminal in dev mode (hot reload) on http://localhost:3001; `make up` already serves :3000
	cd apps/web && npm run dev -- -p 3001

test:
	uv run pytest -q

prove:            ## run all tests and print the invariant evidence table
	uv run python scripts/prove.py

prove-docker:     ## same, in a Linux container (use this on Windows; needs `make up`)
	docker compose run --rm --build prove

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
	@uv run python -c "import httpx, json, time; from quantswarm.api.client import api_headers; time.sleep(3); c=httpx.Client(headers=api_headers()); print(json.dumps({'health': c.get('$(API)/v1/health').json(), 'verify': c.get('$(API)/v1/verify').json()}, indent=1))"

check-integrations: ## which optional keys are configured + one live smoke call each
	uv run python scripts/check_integrations.py

.PHONY: solana-commit verify-solana
solana-commit: ## commit the selected/latest manifest and publish its verified Devnet receipt to the API
	uv run python -m quantswarm.audit.solana commit "$(RUN)" --api $(API)

verify-solana: ## read the memo back from Devnet and compare the canonical manifest fingerprint
	uv run python -m quantswarm.audit.solana verify "$(RUN)"

.PHONY: auth-init audit-init audit-up audit-seal audit-verify
auth-init:
	python scripts/init_auth.py

audit-init: ## create a local test wallet and pin its public address in ignored .env
	uv run python scripts/init_audit.py

audit-up: audit-init ## run the isolated periodic Devnet signer
	docker compose --profile audit up -d --build auditor

audit-seal:
	uv run python -m quantswarm.audit.traces seal

audit-verify:
	uv run python -m quantswarm.audit.traces verify
