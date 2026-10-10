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

## Left to do (priority order)

### M1 — Demo hardening (must do before judging)
- [ ] Fresh-clone run of `make setup && make up && make tournament && make session` against the **Docker** API (the session was last run against a host API, before order ids were scoped per portfolio)
- [ ] Open the terminal in a browser during a session and fix any UI issues (only `next build` was checked; it has not been visually tested)
- [ ] Rehearse the chaos demo: `make crash-worker` mid-session → `make restart-worker` → "Replay & verify" in UI
- [ ] Optional: add the web app to `docker-compose.yml`

### M2 — Sponsor integrations (need keys/funding)
- [ ] Solana: fund the Devnet key printed by `make solana-commit` at https://faucet.solana.com, then `make solana-commit verify-solana`; confirm the UI shows the explorer link
- [ ] OpenAI: set `OPENAI_API_KEY` and run `make tournament` (live LLM path untested; fixture fallback works)
- [ ] ElevenLabs: set `ELEVENLABS_API_KEY`, test `/v1/speak` from the Ask panel
- [ ] Alpaca: set keys and run `scripts/ingest_alpaca.py`, then a tournament on real data (untested)
- [ ] Tiger Data: optionally point `DATABASE_URL` at Tiger Cloud and rerun the demo

### M3 — Performance follow-ups (documented as next bottlenecks)
- [ ] Incremental per-agent exposure/reservation sums (remove O(open orders) scan in risk checks)
- [ ] Recovery checkpoints (replay suffix instead of full log)
- [ ] Re-run `make bench` after any engine change and commit the regenerated `docs/PERFORMANCE.md`

### M4 — Submission
- [ ] Devpost write-up (reuse README "10 questions" + headline numbers)
- [ ] Record a backup demo video
- [ ] Push repo to GitHub (no secrets: `.env` and `data/solana/` are gitignored)
- [ ] Final `make prove` + `make bench` on the submission commit

### Stretch (only if time)
- [ ] Anchor-based on-chain strategy registry (Solana)
- [ ] Kalshi read-only market discovery (cut from core scope)
- [ ] API authentication per agent
