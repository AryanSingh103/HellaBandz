# Coordinated simulation

The Simulation page launches `kind=coordinated`, not the old independent-agent session. It defaults to the original real SIP hourly stock/ETF snapshot when installed. Synthetic fixtures require an explicit selection and are labeled. There are no live brokerage orders or external exchange depth feeds.

Starting creates a fresh $10,000 paper portfolio. Missing or incompatible registry research runs first, using the existing development/validation pipeline. Execution requires completed research with matching dataset hash, policy and strategy-code version. Legacy tournament allocations never fund a coordinated replay. Rejected challengers stay in shadow; if none pass, the passive core receives the $9,000 deployable budget and the remaining cash is reserved.

All strategies read one snapshot. The coordinator nets opposing sibling intents, checks remaining capital/cash, and submits one aggregate order per symbol. Existing minimum trade sizes, cooldowns, daily turnover caps and research cost/turnover promotion gates control churn. No parameters or promotion thresholds were changed to fit the inspected replay.

`GET /v1/coordinated/research?dataset_id=...` exposes current registry eligibility and frozen identities. `GET /v1/coordinated/report?portfolio_id=...` exposes every completed-cycle rule reading, coordinator decision, virtual sleeve, source/data/policy provenance and execution-matched baseline. `/v1/analytics/history` uses those reconciled sleeves rather than treating the aggregate execution owner as a strategy. Financial amounts in the raw report are integer micro quote units.

The baseline advances on precisely the actual completed bars using the same opening cash, deployable capital, risk limits, fees and synthetic paper CLOB. A stopped replay compares only its completed prefix. Scripted crash overlays are explicitly rejected for coordinated replay. Previously inspected history remains exploratory evidence.

Language models can propose bounded research hypotheses and comment on recorded facts. Commentary records its actual provider/model and authorized Gemini-to-OpenAI fallback. It cannot change execution rules or submit orders. Every agent's rule reading is retained even when its target does not change; the live feed shows first/last readings and target changes.

Downloads include the frozen context, funded and unfunded agents, exact virtual holdings/P&L, all coordinator decisions, rule readings, aggregate fills, raw ledger events and baseline. Raw report checkpoints occur every 25 cycles and at normal completion. Failed/interrupted runs disclose checkpoint coverage; they do not substitute a full-period legacy benchmark. Old exports remain readable and are labeled legacy independent-agent sessions.
