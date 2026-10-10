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
**Owns:** `quantswarm/agents/`, `quantswarm/audit/`, `scripts/ingest_alpaca.py`, `.env.example`, `docs/DATA_SOURCES.md`

- [ ] OpenAI: set `OPENAI_API_KEY`, run `make tournament`; fix prompt/parsing issues in the live LLM path (it has only run on the fixture fallback). Check that the manifest shows `source: llm`
- [ ] Crisis responses with a live LLM during `make session`
- [ ] ElevenLabs: set `ELEVENLABS_API_KEY`, test `/v1/speak` from the Ask panel (browser speech fallback already works)
- [ ] Solana: fund the Devnet address printed by `make solana-commit` at https://faucet.solana.com, then `make solana-commit verify-solana`; confirm the explorer link shows in the UI
- [ ] Alpaca: get free keys, run `scripts/ingest_alpaca.py`, then run a tournament on real data with `--dataset data/snapshots/alpaca-...`; record the results in `docs/DATA_SOURCES.md`
- [ ] Optional: point `DATABASE_URL` at Tiger Cloud and rerun the demo (Tiger Data prize)

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
