# Integrations runbook (P2)

All integrations are optional. Without keys the system runs offline: recorded agent proposals,
a deterministic supervisor and crisis policy, browser speech, the synthetic dataset, and offline
manifest verification. Every live path is unit-tested against mocked services
(`tests/unit/test_integrations.py`), so adding a key should work on first use.

**Start here:** `cp .env.example .env`, fill in what you have, then run `make check-integrations`.
Host scripts read `.env` automatically. Docker Compose also reads it for `${...}` substitution;
restart the API after editing it: `docker compose up -d api`.

| Integration | Key(s) | Verify | Then |
|---|---|---|---|
| OpenAI | `OPENAI_API_KEY`, optional `QS_OPENAI_MODEL` (default `gpt-4.1-mini`) | `make check-integrations` → `OK OpenAI` | `make tournament`: the summary table's `src` column should say `llm`; `make session` crisis messages say `source: llm` |
| ElevenLabs | `ELEVENLABS_API_KEY`, optional `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | `OK ElevenLabs … bytes of audio` | Ask panel in the terminal speaks through `/v1/speak` |
| Alpaca | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` (free Basic plan) | `OK Alpaca sip: ok` (or `WARN` ⇒ use `--feed iex`) | see "Real-data tournament" below |
| Solana Devnet | none (keypair auto-created in `data/solana/`, gitignored) | `TODO` until funded, then `OK` | `make solana-commit && make verify-solana` |
| Tiger Cloud | `DATABASE_URL` (host) **and** `QS_DATABASE_URL` (containers), same service URL with `?sslmode=require` | `OK … Tiger Cloud: timescaledb …` | `docker compose up -d worker api` (skip `db`), then `make tournament session` |

## OpenAI behavior

- Research agents get **training-split statistics only** and the whitelisted families/bounds. Answers are
  validated (JSON object, family allowed for that agent, symbols present in the dataset, at least one grid
  point inside bounds). One retry is made with the validation error appended; after two bad answers, or on any
  network/auth/quota error, the agent falls back to its recorded proposal. The manifest records `source`,
  attempts, model, latency, tokens and the errors.
- The supervisor's weights are advisory: non-numeric entries are dropped, and the deterministic allocator
  limits it to ±20 pp of score-proportional weights, so it can never fund a strategy with score 0.
- Crisis responses must be one of `buy_dip | reduce | hold` with `fraction` clamped to [0, 1]; anything else
  falls back. The resulting orders still go through the risk engine.
- Reasoning models (`o1`, `o3`, `o4`, `gpt-5*`) are called without `temperature`.
- Cost: one tournament makes 4 small calls (3 research agents + supervisor), a session makes 3 more (crisis), each a few
  hundred tokens.

## Solana

1. `make check-integrations` prints the Devnet address (also created on first `make solana-commit`).
2. Fund it at https://faucet.solana.com (choose Devnet). The RPC airdrop is often rate-limited, which is why
   this step is manual. 0.01 SOL covers hundreds of memo transactions.
3. `make solana-commit`: hashes the latest tournament manifest (RFC 8785-style canonical JSON, SHA-256), sends
   one SPL Memo transaction `quantswarm:v1:<run_id>:<sha256>`, writes `data/manifests/<run>.solana.json`,
   and stores the signature with the tournament so the terminal shows the explorer link.
4. `make verify-solana`: recomputes the hash from the local file and compares it with the memo read back
   from chain. Changing manifest content fails verification (exit code 1); JSON formatting does not affect the canonical hash.

Select a specific run with `make solana-commit RUN=data/manifests/<run>.json`. A retry reuses a saved,
confirmed matching transaction, including when API publication previously failed. API errors are reported
instead of silently ignored. The API validates the confirmed memo before storing the signature;
signature-free research retries retain the saved proof. Research shows an Explorer link and a
**Verify on-chain fingerprint** button, backed by `GET /v1/tournaments/<run>/solana`.
An unsigned run is shown as uncommitted, and a mismatching or failed transaction never verifies.

## Real-data tournament (Alpaca)

```bash
uv run python scripts/ingest_alpaca.py --symbols SPY QQQ AAPL MSFT NVDA --start 2023-01-03 --end 2026-09-30
uv run python -m quantswarm.agents.tournament --dataset data/snapshots/alpaca-sip-1hour-2023-01-03-2026-09-30 --api http://localhost:8000
uv run python -m quantswarm.agents.session --dataset data/snapshots/alpaca-sip-1hour-2023-01-03-2026-09-30
```

Hourly bars are stamped with their start time. Bars starting 09:00–15:00 New York time are kept (7 per
day). The 09:00 bar includes the 09:30 open plus 30 minutes of pre-market prints. Raw data stays out of git; commit
only the printed `content_sha256` and the results summary in `docs/DATA_SOURCES.md`.

## Tiger Cloud

Create a service in the Tiger Data console and copy its connection string. Put it in `.env` as both
`DATABASE_URL` and `QS_DATABASE_URL` (append `?sslmode=require` if missing). The schema is idempotent and
creates the hypertables on first start. Benchmarks in `docs/PERFORMANCE.md` were measured on local TimescaleDB,
not Tiger Cloud.
