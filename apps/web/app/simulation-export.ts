import { Analytics, CoordinatedReport, Dataset, Job, Manifest, Portfolio, Verification, request } from "./model";

type LedgerRow = { seq: number; type: string; created_at: string; event: Record<string, unknown> & { type: string } };
type MessageRow = { id: number; agent_id: string; role: string; content: Record<string, unknown>; created_at: string; portfolio_id: string | null };
type EquityRow = { ts: string; equity: number; cash: number; market_value: number };
type Spec = { strategy_id: string; agent_id?: string; family?: string; universe: string[]; params?: Record<string, number> };

const quote = (micros: unknown) => typeof micros === "number" ? micros / 1e6 : undefined;
const iso = (ts: number) => new Date(ts * 1000).toISOString();
const optional = <T,>(p: Promise<T>) => p.catch(() => null);

async function paged<T>(path: string, params: Record<string, string | number>, cursor: string, key: (row: T) => number, size: number) {
  const out: T[] = [];
  for (let after = 0; ;) {
    const rows = await request<T[]>(path, { ...params, [cursor]: after, limit: size });
    out.push(...rows);
    if (rows.length < size) return out;
    after = key(rows[rows.length - 1]);
  }
}

/** Everything recorded about one simulation, re-read from the API at the moment of the click and laid out for reading. */
export async function buildSimulationExport(pid: string, manifest: Manifest | null, dataset: Dataset | undefined, job: Job | undefined, how: Record<string, string>) {
  const [ledger, messages, equity, analytics, port, verify] = await Promise.all([
    paged<LedgerRow>("/v1/events", { portfolio_id: pid }, "after_seq", r => r.seq, 5000),
    paged<MessageRow>("/v1/agent-messages", { portfolio_id: pid }, "after_id", r => r.id, 1000),
    request<EquityRow[]>("/v1/equity", { portfolio_id: pid, limit: 50000 }),
    optional(request<Analytics>("/v1/analytics/history", { portfolio_id: pid })),
    optional(request<Portfolio>("/v1/portfolio", { portfolio_id: pid })),
    optional(request<Verification>("/v1/verify", { portfolio_id: pid })),
  ]);
  if (job?.kind === "coordinated" || messages.some(m => m.role === "coordinator_start")) {
    // Required, rather than optional: a legacy manifest or the execution owner's
    // position is never an acceptable substitute for coordinated sleeve evidence.
    const report = await request<CoordinatedReport>("/v1/coordinated/report", { portfolio_id: pid });
    const last = report.steps.at(-1), books = report.final_attribution || last?.sleeves || {};
    const complete = report.status === "completed" && job?.status === "completed";
    return {
      about: { what: "Coordinated stock/ETF historical paper replay; pretend money.", portfolio_id: pid,
        exported_at: new Date().toISOString(), still_running: ["queued", "running"].includes(job?.status || ""),
        monetary_units: "metrics and agent summaries below are quote units; coordinated_report and raw_ledger_events use integer micro quote units (1,000,000 = 1 USD).",
        execution: "Frozen strategy rules emit intents. One coordinator nets sibling intents, preflights remaining allocation, and owns aggregate venue orders. Virtual sleeves reconcile to that execution ledger.",
        commentary: "Language-model remarks are commentary only. Each remark records its actual provider/model, including Gemini to OpenAI failover.",
        history_status: report.context.evaluation_status, depth: report.context.depth,
        allocation_mode: report.context.allocation_mode || "approved", allocation_note: report.context.allocation_note },
      simulation: { job_id: job?.job_id, status: job?.status || report.status,
        config: (job as Job & { config?: unknown } | undefined)?.config, summary: report.summary,
        started_at: job?.started_at, finished_at: job?.finished_at, dataset: report.context.dataset,
        completed_requested_replay: complete, actual_range: { start_ts: report.benchmark.start_ts, end_ts_exclusive: report.benchmark.end_ts },
        coverage: { coordinated_through_ledger_seq: report.points.at(-1)?.seq, exported_ledger_seq: port?.seq,
          report_finalized: !!report.summary, note: "During a run, coordinated reports checkpoint every 5 cycles. Failed/interrupted exports retain the last checkpoint; later raw ledger events may exist. Full-horizon metrics require a finalized summary." } },
      provenance: report.context,
      metrics: { starting_cash: quote(port?.state.initial_cash), account_value: quote(port?.equity),
        return: port && port.state.initial_cash ? port.equity / port.state.initial_cash - 1 : undefined,
        fees: quote(port?.state.fees_paid), slippage: quote(port?.state.slippage_cost),
        strategy: report.benchmark.strategy_metrics, baseline: report.benchmark.baseline_metrics,
        ledger_check: verify },
      agents: report.context.agents.map(a => ({ agent: a.agent_id, strategy_id: a.strategy_id,
        frozen_version: a.version, rule_type: a.family, rule_settings: a.params, symbols: a.universe,
        funded_in_this_simulation: a.funded, budget: quote(a.capital), hypothesis: a.hypothesis,
        promotion_status: a.status, promotion_reasons: a.reasons, research_source: a.proposal_source,
        experimental_paper_budget: !!a.experimental, research_approved: a.research_approved ?? a.funded,
        validation_evidence: a.evaluation, profit_or_loss_at_end: quote(books[a.agent_id]?.pnl) || 0,
        fees: quote(books[a.agent_id]?.fees) || 0, slippage: quote(books[a.agent_id]?.slippage) || 0,
        attributed_fills: books[a.agent_id]?.fills || 0,
        holdings_at_end: Object.fromEntries(Object.entries(books[a.agent_id]?.positions || {}).map(([sym, p]) => [sym, { qty: p.qty, cost_basis: quote(p.basis) }])) })),
      thinking: [...report.rule_readings.map(r => ({ kind: "rule_reading", ...r })),
        ...messages.filter(m => m.role === "thought" || m.role === "thinking_unavailable").map(m => ({ kind: m.role === "thought" ? "ai_remark" : m.role,
          agent: m.agent_id, recorded_at: m.created_at, ...m.content }))],
      decisions: report.decisions,
      trades: ledger.filter(r => r.event.type === "OrderFilled").map(r => ({ ...r.event, ledger_seq: r.seq,
        note: "Aggregate execution fill; per-strategy holdings, fees and P&L are in coordinated_report.steps[].sleeves." })),
      steps: report.steps, buy_and_hold_comparison: report.benchmark,
      agent_profit_history: analytics, coordinated_report: report, raw_ledger_events: ledger,
      session_messages: messages, final_account_state: port?.state,
    };
  }
  const account = new Map(equity.map(p => [Date.parse(p.ts) / 1000, p]));
  const steps: Record<string, unknown>[] = [], decisions: Record<string, unknown>[] = [], trades: Record<string, unknown>[] = [];
  const held: Record<string, Record<string, number>> = {}, placed = new Map<string, Record<string, unknown>>();
  const counts: Record<string, { approved: number; reduced: number; rejected: number; fills: number }> = {};
  const count = (agent: unknown) => counts[String(agent)] ??= { approved: 0, reduced: 0, rejected: 0, fills: 0 };
  const holdings = () => Object.fromEntries(Object.entries(held).map(([a, s]) => [a, Object.fromEntries(Object.entries(s).filter(([, q]) => q !== 0))]).filter(([, s]) => Object.keys(s as object).length));
  let ts = 0, current: Record<string, unknown> | null = null;
  for (const row of ledger) {
    const e = row.event;
    if (e.type === "BarApplied") {
      if (e.ts !== ts) {
        if (current) current.holdings_at_end_of_step = holdings();
        ts = Number(e.ts);
        const a = account.get(ts);
        current = { step: steps.length + 1, market_time: iso(ts), closes: {}, account_value: quote(a?.equity), cash: quote(a?.cash), invested_value: quote(a?.market_value) };
        steps.push(current);
      }
      (current!.closes as Record<string, unknown>)[String(e.symbol)] = quote(e.close);
      continue;
    }
    const order = typeof e.order_id === "string" ? e.order_id : "";
    if (order && (e.type === "OrderAccepted" || e.type === "OrderRejected")) placed.set(order, e);
    const origin = placed.get(order) || {};
    const agent = e.agent_id ?? origin.agent_id, symbol = e.symbol ?? origin.symbol, side = e.side ?? origin.side;
    const base = { ledger_seq: row.seq, step: steps.length, market_time: ts ? iso(ts) : null, type: e.type, agent, symbol, side };
    if (e.type === "OrderAccepted") {
      count(agent)[e.outcome === "REDUCE" ? "reduced" : "approved"]++;
      decisions.push({ ...base, requested_qty: e.requested_qty, approved_qty: e.qty, risk_decision: e.outcome, risk_rules: e.rules, order_type: e.order_type, crash_response: order.includes(".crisis"), order_id: order });
    } else if (e.type === "OrderRejected") {
      count(agent).rejected++;
      decisions.push({ ...base, requested_qty: e.requested_qty, approved_qty: 0, risk_decision: "REJECT", risk_rules: e.rules, crash_response: order.includes(".crisis"), order_id: order });
    } else if (e.type === "OrderFilled") {
      count(agent).fills++;
      const book = held[String(agent)] ??= {};
      book[String(symbol)] = (book[String(symbol)] || 0) + (side === "BUY" ? 1 : -1) * Number(e.qty);
      trades.push({ ...base, qty: e.qty, price: quote(e.px), fee: quote(e.fee), slippage: quote(e.slippage), position_after: book[String(symbol)], order_id: order });
    } else {
      decisions.push({ ...base, reason: e.reason, budget: quote(e.capital), initial_cash: quote(e.initial_cash), order_id: order || undefined });
    }
  }
  if (current) current.holdings_at_end_of_step = holdings();

  const mine = messages.filter(m => m.portfolio_id === pid);
  const thinking = mine.filter(m => ["signal", "thought", "crisis_response"].includes(m.role)).map(m => ({
    id: m.id, agent: m.agent_id, kind: m.role === "signal" ? "rule_reading" : m.role === "thought" ? "ai_remark" : "crash_decision",
    step: m.content.step, market_time: typeof m.content.ts === "number" && m.content.ts ? iso(m.content.ts) : undefined, recorded_at: m.created_at, ...m.content,
  }));
  const state = port?.state;
  const pnl = analytics?.points.at(-1)?.agents || {};
  const specs: Spec[] = manifest ? [...manifest.fitted.map(f => f.spec as Spec), ...(manifest.core ? [manifest.core.spec as Spec] : [])] : [];
  const agents = specs.map(s => {
    const agent = s.agent_id || "core", ranked = manifest!.ranked.find(r => r.strategy_id === s.strategy_id), proposal = manifest!.proposals.find(p => p.agent_id === agent);
    const family = s.family || ranked?.family || (manifest!.core?.spec.strategy_id === s.strategy_id ? "buy_hold" : undefined);
    const budget = state?.allocations[agent];
    return {
      agent, role: family === "buy_hold" ? "passive core (no AI, buys everything equally and holds)" : "AI-proposed strategy", strategy_id: s.strategy_id, rule_type: family,
      how_it_trades: how[family || ""], rule_settings: s.params, symbols: s.universe,
      research: proposal ? { proposed_by: proposal.source, thesis: proposal.proposal.rationale, evidence: proposal.proposal.evidence } : undefined,
      judged_on_unseen_data: ranked ? { validation: ranked.validation, training: ranked.train, score: ranked.score, failed_checks: ranked.gates, excess_vs_core: ranked.excess } : undefined,
      research_allocation: manifest!.allocations.by_strategy[s.strategy_id],
      funded_in_this_simulation: (budget || 0) > 0, budget_at_end: quote(budget),
      holdings_at_end: Object.fromEntries(Object.entries(state?.positions || {}).filter(([k, p]) => k.split("|")[0] === agent && p.qty !== 0).map(([k, p]) => [k.split("|")[1], { qty: p.qty, cost_basis: quote(p.basis) }])),
      profit_or_loss_at_end: quote(pnl[agent]?.pnl), orders: counts[agent] || { approved: 0, reduced: 0, rejected: 0, fills: 0 },
    };
  });
  const values = [...equity].reverse().map(p => p.equity);
  let peak = 0, drawdown = 0;
  for (const v of values) { peak = Math.max(peak, v); if (peak) drawdown = Math.max(drawdown, (peak - v) / peak); }
  const initial = state?.initial_cash || manifest?.policy.initial_cash_micros;
  const total = (k: "approved" | "reduced" | "rejected" | "fills") => Object.values(counts).reduce((n, c) => n + c[k], 0);
  return {
    about: {
      what: "Legacy independent-agent session (pretend money, historical replay); not the coordinated registry/netting runtime.",
      portfolio_id: pid, exported_at: new Date().toISOString(), still_running: !!job && ["queued", "running"].includes(job.status),
      how_to_read: {
        money: `All money and prices are in ${dataset?.quote_currency || "USD"} (already converted from the ledger's micro-units).`,
        quantities: (dataset?.lots_per_base_unit || 1) > 1 ? `Quantities are execution lots; ${dataset!.lots_per_base_unit} lots = 1 base unit.` : "Quantities are whole shares.",
        agents: "Each agent trades by a fixed rule chosen during research. An agent with funded_in_this_simulation=false has a $0 budget: it places no orders and holds nothing; only what its rule WOULD do is recorded under thinking.",
        thinking: "rule_reading = indicator values behind a change in the rule's target (computed from the same price bars). ai_remark = a short language-model comment on the listed facts; it cannot place orders. crash_decision = the model's (or scripted fallback's) response to an injected crash.",
        decisions: "Every order an agent's rule sent and what the independent risk engine decided (APPROVE / REDUCE / REJECT with rule codes), plus account events.",
        trades: "Fills only. position_after is that agent's holding in that symbol right after the fill.",
        steps: "One row per replayed market timestamp: closing prices, account totals, and every agent's holdings at the end of that step.",
      },
    },
    simulation: { job_id: job?.job_id, status: job?.status || "recorded session (no job record)", config: (job as { config?: unknown } | undefined)?.config, summary: job?.result, started_at: job?.started_at, finished_at: job?.finished_at,
      research_run: manifest?.run_id, dataset: manifest?.dataset, units: dataset ? { quote_currency: dataset.quote_currency, lots_per_base_unit: dataset.lots_per_base_unit } : undefined },
    metrics: {
      starting_cash: quote(initial), account_value: quote(port?.equity), return: port && initial ? port.equity / initial - 1 : undefined, cash: quote(state?.cash), invested_value: quote(state?.market_value),
      invested_fraction: port && state ? state.market_value / port.equity : undefined, worst_drop_from_peak: drawdown, fees: quote(state?.fees_paid), slippage: quote(state?.slippage_cost), realized_profit: quote(state?.realized_pnl),
      orders: { approved: total("approved"), reduced: total("reduced"), rejected: total("rejected") }, fills: total("fills"), steps_replayed: steps.length, kill_switch: state?.kill_switch,
      ledger_check: verify ? { events: verify.events, replay_matches_stored_account: verify.match, hash_chain_intact: verify.chain_ok } : undefined,
    },
    agents,
    research: manifest ? { supervisor: manifest.supervisor, sealed_final_exam_result: manifest.held_out_test.portfolio.metrics, policy: manifest.policy } : null,
    thinking,
    decisions,
    trades,
    steps,
    session_messages: mine.filter(m => !["signal", "thought", "crisis_response"].includes(m.role)).map(m => ({ id: m.id, role: m.role, agent: m.agent_id, recorded_at: m.created_at, ...m.content })),
    agent_profit_history: analytics ? { note: "Sampled from the ledger by the server; sample_stride shows the spacing.", ...(analytics as unknown as Record<string, unknown>), benchmark: undefined } : null,
    buy_and_hold_comparison: analytics?.benchmark ?? null,
    final_account_state: state ?? null,
  };
}

export function downloadJson(name: string, value: unknown) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(value, null, 1)], { type: "application/json" }));
  link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}
