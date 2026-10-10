# Milestones

Status as of 2026-10-10 (end of first build session). See `PLAN.md` for the full plan.

## Done and verified

- [x] Deterministic ledger core (`quantswarm/ledger`): risk rules R0–R9, bar-based fill model, integer money, hash-chained events, invariant checker, replay
- [x] Execution worker (single writer, advisory lock, atomic batch commits, startup replay + digest check)
- [x] FastAPI gateway: idempotent command intake, 409 on payload conflict, 202 when worker down, read models, `/v1/verify`, WebSocket stream, `/v1/ask`, `/v1/speak`
- [x] TimescaleDB schema (hypertables: fills, equity_points, market_bars, telemetry)
- [x] Backtester on the same engine (Lucera interfaces), 4 whitelisted causal strategy families
- [x] Synthetic fixture dataset (committed, hashed); Binance ingestion (checksummed, 131k bars); Alpaca ingestion script
- [x] LangGraph tournament (3 research agents → train grid search → validation ranking + stability + gates → supervisor → allocator → held-out test once → hashed manifest). Runs offline in ~15 s
- [x] Live session runner (held-out replay via API, crash overlay, agent crisis responses, flaky-client retries)
- [x] Tests: 56 passing (unit, property, real-Postgres concurrency, SIGKILL recovery, tamper detection); `make prove` → all 11 invariants PROVEN
- [x] Benchmarks + generated `docs/PERFORMANCE.md` (risk check p50 2.9 µs; ~1,050 orders/s at 50 clients; 100k-event recovery 1.6 s)
- [x] Docs: README (Lucera 10 questions, quickstart, demo script, disclosure), ARCHITECTURE, INVARIANTS, DATA_SOURCES, LIMITATIONS
- [x] Next.js terminal (`apps/web`): builds; panels for portfolio, tournament, agents, orders, adversarial console, ask/voice, event inspector
- [x] Solana memo commit/verify code; offline tamper test passes

## Remaining work, split three ways

Each part owns different folders so the three of you can work in parallel with few merge conflicts.
Work on a branch per part (`p1-demo`, `p2-integrations`, `p3-core`) and open PRs into `main`.
Before merging anything, run `make prove` (all tests must stay green).

### P1 — Demo & Terminal (owner: ______)
**Owns:** `apps/web/`, `docker-compose.yml`, `Makefile`, `README.md` demo section

- [ ] Fresh clone on a clean machine: `make setup && make up && make tournament && make session`. Fix anything that breaks (the session was last run against a host API, before order ids were scoped per portfolio)
- [ ] Open http://localhost:3000 during a session and fix UI issues (only `next build` was checked; it has not been visually tested)
- [ ] Add the web app to `docker-compose.yml` so `make up` starts everything
- [ ] Rehearse the chaos demo: `make crash-worker` mid-session → `make restart-worker` → "Replay & verify" in the UI
- [ ] Rehearse the full 5-minute demo script in the README; time it
- [ ] Record a backup demo video

**Done when:** a teammate who did not build it can run the whole demo from the README without help.

### P2 — AI & Sponsor Integrations (owner: ______)
**Owns:** `quantswarm/agents/`, `quantswarm/audit/`, `scripts/ingest_alpaca.py`, `.env.example`, `docs/DATA_SOURCES.md`, `docs/INTEGRATIONS.md`
**Runbook:** `docs/INTEGRATIONS.md`. **Status check:** `make check-integrations`.

Done (branch `p2-integrations`):
- [x] `.env` auto-loading for API, agents, Solana and Alpaca scripts (real environment wins)
- [x] Live LLM path hardened and tested against a mocked OpenAI: proposal validation (family, bounds, **symbols must exist in the dataset**, which used to crash), one retry with the error, fixture fallback on bad answers or network errors, sanitized supervisor weights, validated and clamped crisis actions, no `temperature` for reasoning models
- [x] Fixture fallback adapts its universe to any dataset (works on Alpaca/Binance snapshots)
- [x] ElevenLabs `/v1/speak` tested (501 without key, MP3 passthrough with key)
- [x] Solana commit tested end to end against a mocked RPC: signed memo tx, on-chain memo read-back, tamper detection
- [x] Alpaca fetch tested: pagination, 429 retry, clear 401/403 message (suggests `--feed iex`), session-hour filter (fixed docs: the 09:00 bar holds the 09:30 open)
- [x] Tiger Cloud wiring: `QS_DATABASE_URL` for containers, detection in `make check-integrations`
- [x] 19 new tests; full suite 75/75 passing

Needs your keys/funding (code is ready; each item takes a few minutes):
- [x] OpenAI: live agents, supervisor, crisis responses and `/v1/ask` verified (gpt-4.1-mini)
- [x] ElevenLabs: `/v1/speak` returns audio (default voice switched to premade "Daniel"; free plans can't use library voices via API)
- [ ] Fund the Devnet address `5LvRiXCo8r2J6soVfRgziyE7CRDK3tGepKkpWsVCPCiw` at https://faucet.solana.com (RPC airdrop is rate-limited) → `make solana-commit verify-solana`. Note: the keypair is local to this machine (`data/solana/`, gitignored); another machine gets its own address
- [x] Alpaca: real SIP data ingested (32,865 bars), LLM tournament + live session on it, results in `docs/DATA_SOURCES.md`
- [ ] Optional: Tiger Cloud service URL → rerun the demo

**Done when:** each integration works with a key and still falls back cleanly without one.

### P3 — Core Performance & Submission (owner: ______)
**Owns:** `quantswarm/ledger/`, `quantswarm/execution/`, `benchmarks/`, `tests/`, `docs/` (except DATA_SOURCES)

- [ ] Incremental per-agent exposure and reservation sums in `apply_event` (removes the O(open orders) scan in risk checks); add a test that compares them to the full recomputation
- [ ] Recovery checkpoints: snapshot state + digest every N events, replay only the suffix on startup; keep the digest check
- [ ] Re-run `make bench` after the engine changes and commit the regenerated `docs/PERFORMANCE.md`
- [ ] Update `docs/LIMITATIONS.md` / `ARCHITECTURE.md` "next limit" sections to match
- [ ] Devpost write-up (reuse the README's 10-question table and the headline numbers)
- [ ] Make the repo public (or give judges access); final `make prove` + `make bench` on the submission commit

**Done when:** benchmarks are re-measured on the final code and the Devpost page is submitted.

### Coordination notes
- `quantswarm/api/app.py` is shared: P1 (UI needs) and P2 (`/v1/ask`, `/v1/speak`) may both touch it. Keep those edits small and merge often.
- P3's engine changes affect benchmark numbers and recovery behavior. Tell P1 before merging so the demo is rehearsed on the final code.
- Never commit `.env` or `data/solana/` (both are gitignored).

### Stretch (only if all three parts are done)
- [ ] Anchor-based on-chain strategy registry (Solana)
- [ ] Kalshi read-only market discovery
- [ ] Per-agent API authentication
