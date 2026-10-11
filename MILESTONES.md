# Milestones

Status as of 2026-10-10: P1/P2/P3 code combined on `p1-demo`; 92 tests pass on Windows and Linux.
See `docs/VERIFICATION.md` for combined evidence and `docs/SUBMISSION.md` for remaining submission gates.

## Done and verified

- [x] Deterministic ledger core (`hellabandz/ledger`): risk rules R0–R9, bar-based fill model, integer money, hash-chained events, invariant checker, replay
- [x] Execution worker (single writer, advisory lock, atomic batch commits, startup replay + digest check)
- [x] FastAPI gateway: idempotent command intake, 409 on payload conflict, 202 when worker down, read models, `/v1/verify`, WebSocket stream, `/v1/ask`, `/v1/speak`
- [x] TimescaleDB schema (hypertables: fills, equity_points, market_bars, telemetry)
- [x] Backtester on the same engine (Lucera interfaces), 4 whitelisted causal strategy families
- [x] Synthetic fixture dataset (committed, hashed); Binance ingestion (checksummed, 131k bars); Alpaca ingestion script
- [x] LangGraph tournament (3 research agents → train grid search → validation ranking + stability + gates → supervisor → allocator → held-out test once → hashed manifest). Runs offline in ~15 s
- [x] Live session runner (held-out replay via API, crash overlay, agent crisis responses, flaky-client retries)
- [x] Tests: 92 passing on the combined branch (unit, property, real-Postgres concurrency, SIGKILL recovery, tamper detection); `make prove` and `make prove-docker` → all 11 invariants PROVEN
- [x] Combined-source benchmarks and generated `docs/PERFORMANCE.md`; all 12 durable runs and eight recovery comparisons match replay
- [x] Docs: README (Lucera 10 questions, quickstart, demo script, disclosure), ARCHITECTURE, INVARIANTS, DATA_SOURCES, LIMITATIONS
- [x] Next.js terminal (`apps/web`): builds; panels for portfolio, tournament, agents, orders, adversarial console, ask/voice, event inspector
- [x] Solana memo commit/verify code; offline tamper test passes

## Remaining work, split three ways

Each part owns different folders so the three of you can work in parallel with few merge conflicts.
Work on a branch per part (`p1-demo`, `p2-integrations`, `p3-core`) and open PRs into `main`.
Before merging anything, run `make prove` (all tests must stay green).

### P1 — Demo & Terminal (owner: ______)
**Owns:** `apps/web/`, `docker-compose.yml`, `Makefile`, `README.md` demo section

- [x] Fresh clone on a clean machine: `make setup && make up && make tournament && make session`. Fix anything that breaks (the session was last run against a host API, before order ids were scoped per portfolio)
- [x] Open http://localhost:3000 during a session and fix UI issues (only `next build` was checked; it has not been visually tested)
- [x] Add the web app to `docker-compose.yml` so `make up` starts everything
- [x] Rehearse the chaos demo: `make crash-worker` mid-session → `make restart-worker` → "Replay & verify" in the UI


**Done when:** a teammate who did not build it can run the whole demo from the README without help.

### P2 — AI & Sponsor Integrations (owner: ______)
**Owns:** `hellabandz/agents/`, `hellabandz/audit/`, `scripts/ingest_alpaca.py`, `.env.example`, `docs/DATA_SOURCES.md`, `docs/INTEGRATIONS.md`
**Runbook:** `docs/INTEGRATIONS.md`. **Status check:** `make check-integrations`.

Done (branch `p2-integrations`):
- [x] `.env` auto-loading for API, agents, Solana and Alpaca scripts (real environment wins)
- [x] Live LLM path hardened and tested against a mocked OpenAI: proposal validation (family, bounds, **symbols must exist in the dataset**, which used to crash), one retry with the error, fixture fallback on bad answers or network errors, sanitized supervisor weights, validated and clamped crisis actions, no `temperature` for reasoning models
- [x] Fixture fallback adapts its universe to any dataset (works on Alpaca/Binance snapshots)
- [x] ElevenLabs `/v1/speak` tested (501 without key, MP3 passthrough with key)
- [x] Solana commit tested end to end against a mocked RPC: signed memo tx, on-chain memo read-back, tamper detection
- [x] Alpaca fetch tested: pagination, 429 retry, clear 401/403 message (suggests `--feed iex`), session-hour filter (fixed docs: the 09:00 bar holds the 09:30 open)
- [x] Tiger Cloud wiring: `QS_DATABASE_URL` for containers, detection in `make check-integrations`
- [x] 19 integration tests added; combined P1/P2/P3 suite now 92/92 passing

Needs your keys/funding (code is ready; each item takes a few minutes):
- [x] OpenAI: live agents, supervisor, crisis responses and `/v1/ask` verified (gpt-4.1-mini)
- [x] ElevenLabs: `/v1/speak` returns audio (default voice switched to premade "Daniel"; free plans can't use library voices via API)
- [x] Solana: tournament T20261010194733 committed on Devnet and verified (tx 5uJFSVSQ…gamXE, see data/manifests/T20261010194733.solana.json)
- [x] Alpaca: real SIP data ingested (32,865 bars), LLM tournament + live session on it, results in `docs/DATA_SOURCES.md`
- [ ] Optional: Tiger Cloud service URL → rerun the demo

**Done when:** each integration works with a key and still falls back cleanly without one.

### P3 — Core Performance & Submission (owner: ______)
**Owns:** `hellabandz/ledger/`, `hellabandz/execution/`, `benchmarks/`, `tests/`, `docs/` (except DATA_SOURCES)

- [x] Incremental per-agent exposure and reservation sums in `apply_event` (removes the O(open orders) scan in risk checks); add a test that compares them to the full recomputation
- [x] Recovery checkpoints: snapshot state + digest every N events, replay only the suffix on startup; keep the digest check
- [x] Re-run `make bench` after the engine changes and commit the regenerated `docs/PERFORMANCE.md`
- [x] Update `docs/LIMITATIONS.md` / `ARCHITECTURE.md` "next limit" sections to match
- [x] Devpost write-up (reuse the README's 10-question table and the headline numbers)
- [ ] Make the repo public (or give judges access); final `make prove` + `make bench` on the submission commit

**Combined status:** P1/P2/P3 code is merged on `p1-demo`. Combined tests and demo/integration checks pass;
combined benchmarks are re-measured and passing. Judge access and Devpost publication remain unverified;
see `docs/SUBMISSION.md`.

**Done when:** benchmarks are re-measured on the final code and the Devpost page is submitted.

### Coordination notes
- `hellabandz/api/app.py` is shared: P1 (UI needs) and P2 (`/v1/ask`, `/v1/speak`) may both touch it. Keep those edits small and merge often.
- P3's engine changes affect benchmark numbers and recovery behavior. Tell P1 before merging so the demo is rehearsed on the final code.
- Never commit `.env` or `data/solana/` (both are gitignored).

### Stretch (only if all three parts are done)
- [ ] Anchor-based on-chain strategy registry (Solana)
- [ ] Kalshi read-only market discovery
- [ ] Per-agent API authentication
