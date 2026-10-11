# Judge demo

Open [the local terminal](http://localhost:3000). The current terminal is a production
build. It opens without sign-in unless `QS_AUTH_REQUIRED=1` is set.

## Three-minute walkthrough

1. **Research / Agents:** show a frozen tournament and its recorded agent messages.
   Explain train, validation and held-out results; report the displayed result even if negative.
2. **Portfolio:** show simulated orders, fills and balances. The execution worker applies
   deterministic risk rules; an agent proposes a trade but cannot bypass those rules.
3. **Marketplace:** compare return, Sharpe and maximum drawdown, then show strategy licenses
   and completed sales. These transfers use demo credits, with no real payments.
4. **Nike Review Radar:** show dated review evidence and the cutoff. Describe this as an
   unbacktested research tool, not a verified story that Claude predicted Nike's crash.
5. **Audit:** click **Verify blockchain & records**, then **Open Solana transaction**.
   The first full-trace seal covers 9,603 records across 16 record sets, including
   106 agent messages and two completed strategy sales. Explain that the hash on Devnet
   makes later edits detectable; it does not certify truthful research or profitable trades.

The running auditor seals subsequent changes periodically. New activity is provisional
until its next seal. Use free Devnet SOL only.

## Local demo data

The active database is `quantswarm_judges`. It retains the original `main` and
`real-check-20261010` portfolios, their commands and hash-chained events, research,
market data and marketplace records. The original `quantswarm` database, including
benchmark history, remains intact. The first seal is a new baseline for the copied
demo records; it does not retroactively authenticate their pre-seal history.

Current access and trust limits are in [SECURITY](SECURITY.md). Existing performance
tables are labeled historical; the exhaustive security-build benchmark was stopped
to prioritize the working presentation. Do not quote those tables as current throughput.
Keep future benchmark workloads in a separate database, with matching host `DATABASE_URL`
and container `QS_DATABASE_URL`, so the judge demo stays focused on demo activity.

For another machine, follow the README setup and use its own demo database/wallet.
Local operator credentials and the Devnet private key are never committed. Judge
repository access and Devpost publication remain separate [submission gates](SUBMISSION.md).

After merging the HellaBandz package rename, executable modules use `hellabandz`.
Operator sign-in is off by default; set `QS_AUTH_REQUIRED=1` to demonstrate it.
Restart services after merging UI or backend changes to display the updated build.
