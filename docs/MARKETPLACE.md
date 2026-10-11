# Strategy marketplace and Nike Review Radar

Open **Marketplace** at http://localhost:3000/?view=marketplace. This is a local
strategy-license exchange using virtual credits, separate from simulated trading cash.
Switch between `demo-buyer` and `demo-seller` to demonstrate a trade between users.
These identities are shared and unauthenticated, not real customer accounts.

## Buy, compare and resell

Each demo identity receives 10,000 credits once. API startup and **Publish latest
research** import frozen tournament strategies, each with 100 transferable demo
licenses initially offered by `research-desk` at 100 credits each. Refreshing or
restarting never replenishes inventory or credits. The price and supply are demo
conventions, not valuations or proof of ownership of third-party intellectual property.

1. Compare up to four strategies using training, validation or held-out test
   returns, Sharpe, maximum drawdown, fills, fees and slippage. Dataset, period,
   failed gates and unavailable metrics remain visible. Mixed datasets/periods
   show a comparability notice. Reported Sharpe uses the existing backtest engine's
   fixed `sqrt(252 * 7)` hourly regular-session annualization, including for crypto;
   it is not an interval-adaptive annualization.
2. Choose a seller, quantity and confirm a license purchase. Credits debit the buyer
   and credit the seller; the licenses enter the buyer's available holdings.
3. Under **My licenses**, set a quantity and price to publish a resale offer.
   Inventory moves into escrow. A different identity can purchase that offer.
   Canceling returns the unsold licenses. **Trade history** records both sides.

The purchased object is access to a frozen specification inside this demo, not a
share of a strategy's profits or a stock trade. Specifications are also publicly
readable in this unauthenticated prototype. No payment gateway, enforceable IP
license, live brokerage connection, exclusivity or production access control exists.

## Transaction contract

All quantities and balances persist in Postgres. Credit amounts use integer
millionths; asks accept positive prices with at most two decimal places. Row locks
serialize inventory changes and lock accounts in stable order. A transaction commits
the receipt, debit, credit and license transfer together or rolls all of them back.
Purchase and listing request IDs are idempotent: identical retries return the
original result, while conflicting bodies receive 409. Cancellation is repeatable.
The UI retains a request ID for retries of the same form after a network failure.

The conservation checks are `sum(balance) = sum(credit_grant)` and, per asset,
`sum(available holdings) + sum(remaining listings) = supply`. These tables do not
modify the execution ledger, positions, allocations or cash. The marketplace has
its own trade receipts, without the execution ledger's hash-chain recovery contract.

Endpoints under `/v1/marketplace`: GET catalog; POST `/sync`; GET `/wallet` with
`account_id`; POST `/buy`; POST `/listings`; POST `/listings/{id}/cancel`.
See http://localhost:8000/docs for validated request schemas.

## Nike Review Radar

The review strategy is listed as **Unbacktested**, with unavailable trading metrics.
Open **review lab**, load explicitly synthetic example reviews or paste your own
JSON records. Each record requires `review_id`, HTTPS/HTTP `source_url`, timezone-aware
`published_at` and `observed_at`, text, and an integer rating from 1 to 5. The demo
does not scrape sites, authenticate publishers or verify the supplied timestamps.

Only reviews published and observed by the chosen cutoff qualify. The recent
window is `(cutoff - 30 days, cutoff]`; the baseline is the preceding 90 days.
Repeated IDs or normalized text count once. At least five unique reviews per window
are required. A sentiment change of -0.4 or below with a negative recent mean yields
`avoid_or_reduce`; other sufficiently sampled inputs yield `hold`; smaller samples
yield `insufficient_data`. These thresholds are an experimental research rule,
not an estimated crash probability or a demonstrated profitable trading strategy.

- **Offline:** rating baseline `(rating - 3) / 2`. It does not claim to read text.
- **Claude:** the Anthropic Messages API classifies text and quality/value/demand
  themes. Set `ANTHROPIC_API_KEY` and optionally `QS_ANTHROPIC_MODEL` in `.env`, then
  rebuild/restart the API. Missing keys give an explicit error; no silent fallback.
- **OpenAI:** uses the existing configured key/model for text classification.
- **Auto:** chooses configured Claude, then OpenAI, then the offline baseline.

LLM review text is untrusted data; it never grants instructions or changes risk
policy. Outputs must contain each eligible ID exactly once and finite sentiment
scores in [-1,1]. Failed provider calls reject the analysis rather than inventing
results. Analyses save provider/model, timestamps, exact evidence, exclusions and
a content digest in `review_analyses`; history survives reloads. LLM classifications
can vary across fresh calls. Each recorded result is inspectable independently.
Selected cloud classifiers receive the imported review text and IDs.

The API routes `/reviews/example`, `/reviews/analyze` and `/reviews/history` are
under the marketplace prefix. Review signals do not place stock orders; the existing
long-only engine could later consume a validated exposure-reduction policy after a
separate point-in-time price backtest. No short orders are added by this feature.

## Sources supplied for the Nike example

Reviewed October 10, 2026. Context is shown separately from customer-review evidence:

- **KOIOS Tech, “Correlation Between Social Media Chatter And Nike's Stock Plunge,”
  TalkMarkets, July 14, 2024** (user-supplied article). Describes chatter around the
  June 2024 earnings decline and reports substantial bot contamination. This is
  retrospective correlation, not a documented Claude forecast. The article's
  mention total and stated daily average also do not reconcile, so they are not
  imported as quantitative training observations.
- [AltIndex Nike sentiment](https://altindex.com/ticker/nke/sentiment): its displayed
  69/100 snapshot measures investor discussion across forums. It is not a customer
  product-review score, a price forecast, or a point-in-time historical dataset.
  The live AI Score differed from the pasted snapshot, illustrating why timestamps
  and saved evidence matter. We do not mix those two scores.
- **Brian Sozzi, “Nike stock has never been more hated by the market,” Yahoo Finance,
  September 29, 2026** (user-supplied article). Reports short interest and business
  concerns; these are news context, not raw customer reviews.
- [Darryn Pollock, Yahoo/BeInCrypto, October 5, 2026](https://finance.yahoo.com/technology/ai/articles/nike-down-47-ai-agents-030718620.html):
  discusses shopping agents and an already-occurring selloff. It does not document
  Claude predicting that decline from reviews.
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)
  is the implementation reference for the optional Claude classifier.

None of the supplied materials verifies the specific “Claude predicted Nike's crash
from reviews” claim. A predictive evaluation still needs archived review observations,
NKE prices, a frozen cutoff before each decision, execution costs and held-out results.
The illustrative dataset is authored synthetic data and is never labeled as historical evidence.

## Validation

`tests/unit/test_reviews.py` checks deterioration, sample gates, causality,
deduplication, chronology, explicit provider selection and fabricated/nonfinite output
rejection. `tests/integration/test_marketplace.py` uses isolated real Postgres databases
for contention, idempotency, conservation, overspending, overselling, cancellation
races, invalid inputs and persistent review history. Run them with:

```bash
uv run pytest -q tests/unit/test_reviews.py tests/integration/test_marketplace.py
```
