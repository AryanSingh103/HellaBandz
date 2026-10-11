# Access and audit integrity

This is a single-operator paper-trading application. API access control and public
Devnet commitments protect access and sealed record integrity within the boundaries below.
They do not establish profitable strategies, truthful AI reasoning or real execution.

## Operator access

`make setup`, `make up` and `make backend` generate an operator key in ignored `.env`
without printing it. Open the terminal and sign in with `QS_API_TOKEN` from that file.
Never put it in `NEXT_PUBLIC_*`, a URL, a commit or a screenshot. Python agent and benchmark
clients attach the bearer key only to QuantSwarm's API; external providers receive their
own provider credentials.

All API routes and WebSocket streams require a valid bearer key or an expiring browser
session, except minimal `/healthz` and the sign-in/status routes. Browser sessions last
eight hours, use HttpOnly/SameSite=Strict cookies, expire on API restart, and can be revoked
with Sign out. HTTPS sessions additionally use Secure cookies. Five failed sign-ins in a
minute temporarily block further attempts from that address. Browser writes and streams
reject origins outside `QS_ALLOWED_ORIGINS`; wildcard origins are refused. Required auth
with a missing/short key refuses startup. `QS_AUTH_REQUIRED=0` is an explicit offline/test
opt-out, not a deployment setting. Local Compose ports bind to 127.0.0.1.

The key grants full operator access. Demo marketplace identities and agent IDs remain
shared labels inside this operator's workspace. There is no customer registration,
per-account ownership enforcement, role-based access, payment processing or legal licensing.
Remote use requires HTTPS, a proper identity service, restricted DB roles and secret management.

## Full trace commitments

Run `make audit-init` to create/reuse an ignored Devnet wallet and pin its public address
in `QS_AUDIT_SIGNER`. Fund it with free test SOL from the official faucet. Then run
`make backend audit-up`. `make audit-seal` publishes a seal immediately and
`make audit-verify` independently checks the latest finalized seal. The terminal's Audit
page exposes verification, queued sealing, a downloadable receipt and Solana Explorer.

The auditor seals 16 record sets: commands, ledger events, portfolio state, orders, fills,
equity points, agent messages, tournaments, portfolio context, jobs, review analyses,
strategy accounts, assets, holdings, listings and completed strategy trades. Telemetry,
process liveness, rebuildable recovery checkpoints and the market-data cache are excluded.
Executed bars are recorded in commands; research manifests carry frozen dataset hashes.
Coverage spans the database, including retained benchmark portfolios. A sealed record count
is not a count of live trades or AI decisions; benchmark and system-check records remain
identified by their portfolio/run IDs.

Every covered DB write records before/after images atomically in `audit_changes`. Ordinary
UPDATE/DELETE/TRUNCATE of that journal and TRUNCATE of covered tables are rejected.
A REPEATABLE READ snapshot produces canonical SHA-256 roots and counts for every record
set and the journal. Explicit sealed journal-ID ranges handle rollbacks and transactions
that commit out of sequence. A compact `quantswarm:audit:v1:<id>:<manifest hash>` SPL Memo
commits the manifest. Raw messages, orders and private keys are never placed on-chain.

Verification discovers the latest finalized commitment from the pinned public wallet,
checks the Devnet genesis hash, successful transaction and fee-payer signature, then
compares the manifest hash. It streams current rows and reverses later journal changes
to reconstruct the sealed snapshot. Verification rejects rewrites to immutable
ledger/fill/message/trade/evidence records, command inputs and research manifests. Legitimate
new trades and projection updates remain provisional until their next seal. Missing latest
receipts, forged journal prefixes, unlogged edits and modified sealed roots fail verification.
The signer verifies the preceding seal again in the same snapshot before sealing new data.

Only the isolated `auditor` service mounts the signing key, read-only. The API and browser
can queue a seal but cannot sign. The periodic service seals changed records at most once
per configured interval (minimum 60 seconds; default 300), coalesces requests, reuses landed
transactions after a crash and persists public receipts in `data/audits/`. Table and journal
hashes stream; verification memory grows with distinct records changed since the seal.

## Trust and remaining limits

- Devnet can reset and public RPCs can fail/rate-limit; it is not a permanent production
  evidence store. Unavailable RPC is reported separately from a mismatch.
- First sealing is a baseline. Data cannot be authenticated retroactively before that
  baseline, and changes after the latest seal are outside its final integrity guarantee.
- A DB administrator can disable triggers. External roots still detect altered sealed
  facts, but unsealed activity is vulnerable. Mutable projections are journaled rather
  than treated as independent authority; use full ledger replay for their economic validity.
- The host, pinned public identity, signer key and verifier code must be trusted. Someone
  controlling the signing key can attest to false data. Keep public receipts independently,
  back up the wallet securely and use a dedicated wallet per database. This is not a
  multi-party consensus scheme or an independently audited security product.
  Verification relies on the configured TLS-protected RPC's blockchain responses; it is
  not a local consensus/light-client verifier.
- Journal/history retention and verification I/O grow with activity. Next: restricted DB
  roles, bounded archive proofs, schema-version migrations, external receipt monitoring,
  managed keys and multi-user authorization before public deployment.

The signing guard compares the actual RPC genesis hash with the official
[Devnet genesis identity](https://beta.docs.solanalabs.com/clusters/available), rather
than trusting a URL label. See [Solana getGenesisHash](https://solana.com/docs/rpc/http/getgenesishash).

`tests/unit/test_access.py` and `tests/integration/test_trace_audit.py` exercise access,
sessions, expiry, origin checks, WebSockets, journal atomicity, commit holes, snapshot
reconstruction, immutable rewrites, privileged edits, rollback and network/signer guards.
