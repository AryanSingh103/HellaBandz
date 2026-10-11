"use client";
import { useMemo, useState } from "react";
import { GitMerge, ShieldAlert, ShieldCheck, Gauge, Layers } from "lucide-react";
import { money, pct, stamp } from "./model";
import { useResource } from "./data";
import { Empty, JsonView, Metric, Notice } from "./ui";

type Lat = { n: number; p50_us?: number; p95_us?: number; p99_us?: number };
type IntentRes = { intent_id: string; strategy_id: string; outcome: string; requested_qty: number; attributed_qty: number; approved_delta: number; reasons: string[] };
type Decision = { cycle: number; ts: number; instrument: string; venue: string; price: number; current_qty: number; aggregate_target_qty: number;
  intents: IntentRes[]; transfers: { from_strategy: string; to_strategy: string; qty: number; px: number }[];
  order: { client_order_id: string; side: string; qty: number; limit_px: number; tif: string; attribution: Record<string, number> } | null;
  cancel_order_id: string | null; portfolio_reasons: string[]; explanation: string };
type Intent = { strategy_id: string; strategy_version: string; instrument: string; target_weight_bps: number; score: number; reasons: string[]; features: Record<string, number> };
type Sleeve = { capital: number; cash: number; equity: number; pnl: number; realized: number; unrealized: number; fees: number; fills: number; positions: Record<string, { qty: number }> };
type State = { mode: string; venue: string; data_class: string; data_source: string; dataset_id: string; cycle: number; market_ts: number;
  freshness: Record<string, { age_s: number; needs_resync: boolean }>; kill_switch: boolean; kill_reason: string;
  account: { equity: number; cash: number; reserved_cash: number; market_value: number; fees: number; slippage: number; positions: Record<string, { qty: number; basis: number; mark: number }> };
  working_orders: { order_id: string; symbol: string; side: string; remaining: number; limit_px: number; venue: string }[];
  fills_by_venue: Record<string, number>; external_fills: number; intents: Intent[]; shadow_intents: Intent[];
  sleeves: Record<string, Sleeve>; unallocated_cash: number;
  strategies: { strategy_id: string; version: string; family: string; params: Record<string, number>; capital: number; hypothesis: string; failure_conditions: string }[];
  champions: Record<string, { version: string; weight: number; family: string; since: string }>;
  registry_history: { at: string; event: string; role: string; version: string; weight?: number; reasons?: string[] }[];
  timings: Record<string, Lat>; ledger_round_trip: Lat; ledger_calls: number; gateway: Record<string, number>;
  depth: Record<string, { bids: { px: number; qty: number }[]; asks: { px: number; qty: number }[]; label: string }>;
  intent_outcomes: Record<string, number> };
type EvalRow = { label: string; net_return: number; max_drawdown: number; sharpe: number; turnover_x_capital: number; funded?: boolean; weight?: number };
type Evaluation = { generated_at?: string; summary?: { markets: number; mean_excess: number; standard_error: number | null; ahead_in: number; equal_in: number };
  datasets?: { dataset_id: string; excess_vs_baseline: number; holdout_untouched: boolean; holdout_views: number; rows: Record<string, EvalRow>; critic: string[] }[] };
type View = { state: { content: State; created_at: string } | null; decisions: { content: Decision; created_at: string }[];
  summary: Record<string, unknown> | null; start: { sleeves: [string, string, number][]; shadow: string[] } | null; evaluation: Evaluation | null };

const OUTCOME_CLASS: Record<string, string> = { accepted: "pos", reduced: "warn", netted: "pos", deferred: "muted", rejected: "neg" };
const us = (l?: Lat) => !l || !l.n ? "—" : `${l.p50_us} / ${l.p95_us} / ${l.p99_us}`;

export function CoordinatorView({ pid }: { pid: string }) {
  const view = useResource<View>("/v1/coordinator", { portfolio_id: pid }, 3000);
  const [sym, setSym] = useState("");
  const v = view.data;
  const s = v?.state?.content;
  const latest = useMemo(() => {
    const out: Record<string, Decision> = {};
    for (const d of v?.decisions || []) if (!out[d.content.instrument]) out[d.content.instrument] = d.content;
    return out;
  }, [v]);
  const instruments = Object.keys(latest).sort();
  const pick = sym && latest[sym] ? sym : instruments[0];
  const d = pick ? latest[pick] : undefined;
  if (view.error) return <Notice error>{view.error}</Notice>;
  if (!v) return <Empty>Loading coordinator...</Empty>;
  return <>
    <div className="section-title"><h1>Coordinator</h1><span className="muted">{pid}{s ? ` / cycle ${s.cycle} / market time ${stamp(s.market_ts)}` : ""}</span></div>
    {!s ? <Empty>No coordinated session on this portfolio yet. Go to Run, pick a research run, and press "Launch coordinated session".</Empty> : <>
      <div className="coord-strip">
        <span className="badge">Mode: {s.mode === "paper_clob" ? "Internal paper CLOB (simulated)" : s.mode}</span>
        <span className="badge">Data: {s.data_class} / {s.dataset_id}</span>
        <span className="badge">Freshest data {Math.min(...Object.values(s.freshness).map(f => f.age_s))}s old{Object.values(s.freshness).some(f => f.needs_resync) ? " / RESYNC NEEDED" : ""}</span>
        <span className={`badge ${s.kill_switch ? "bad" : ""}`}>{s.kill_switch ? <><ShieldAlert size={13} /> Kill switch ON ({s.kill_reason})</> : <><ShieldCheck size={13} /> Kill switch off</>}</span>
        <span className="badge">External fills: {s.external_fills} (paper only)</span>
      </div>
      <div className="metrics"><Metric label="Equity" value={money(s.account.equity)} /><Metric label="Cash" value={money(s.account.cash)} /><Metric label="Reserved cash" value={money(s.account.reserved_cash)} /><Metric label="Market value" value={money(s.account.market_value)} /><Metric label="Fees / slippage" value={`${money(s.account.fees)} / ${money(s.account.slippage)}`} /><Metric label="Intents (accepted / netted / reduced / deferred / rejected)" value={["accepted", "netted", "reduced", "deferred", "rejected"].map(k => s.intent_outcomes[k] || 0).join(" / ")} /></div>

      <section className="card coord-decision"><div className="section-title"><h2><GitMerge size={17} /> How the coordinator decided</h2>
        <div className="row">{instruments.map(i => <button key={i} className={i === pick ? "primary" : ""} onClick={() => setSym(i)}>{i}</button>)}</div></div>
        {!d ? <Empty>No decisions recorded yet.</Empty> : <>
          <p className="coord-sentence">{d.explanation}</p>
          <div className="metrics"><Metric label="Account holds" value={`${d.current_qty} lots`} /><Metric label="Aggregate target" value={`${d.aggregate_target_qty} lots`} /><Metric label="Last close" value={money(d.price)} /><Metric label="Order" value={d.order ? `${d.order.side} ${d.order.qty} @ ${money(d.order.limit_px)} ${d.order.tif}` : "none"} /></div>
          <div className="table-scroll"><table><thead><tr><th>Agent</th><th>Wanted</th><th>Held</th><th>Change given</th><th>Outcome</th><th>Why</th></tr></thead><tbody>
            {d.intents.map(r => <tr key={r.intent_id}><td>{r.strategy_id}</td><td>{r.requested_qty}</td><td>{r.attributed_qty}</td><td>{r.approved_delta > 0 ? "+" : ""}{r.approved_delta}</td><td className={OUTCOME_CLASS[r.outcome]}>{r.outcome}</td><td className="muted">{r.reasons.join(", ") || "—"}</td></tr>)}
          </tbody></table></div>
          {!!d.transfers.length && <p className="muted">Netted internally: {d.transfers.map(t => `${t.qty} from ${t.from_strategy} to ${t.to_strategy} at ${money(t.px)}`).join("; ")}. No market order was needed for that part.</p>}
          {!!d.portfolio_reasons.length && <p className="muted">Portfolio limits and gates: {d.portfolio_reasons.join(", ")}</p>}
          {d.order && <p className="muted">Order {d.order.client_order_id} carries lots for: {Object.entries(d.order.attribution).map(([k, q]) => `${k} ${q}`).join(", ")}.</p>}
        </>}
      </section>

      <section><h2>Agent intents right now</h2><div className="table-scroll"><table><thead><tr><th>Agent</th><th>Version</th><th>Instrument</th><th>Target weight</th><th>Score</th><th>Reasons</th></tr></thead><tbody>
        {s.intents.map(i => <tr key={i.strategy_id + i.instrument}><td>{i.strategy_id}</td><td className="mono">{i.strategy_version}</td><td>{i.instrument}</td><td>{pct(i.target_weight_bps / 10000)}</td><td>{i.score.toFixed(3)}</td><td className="muted">{i.reasons.join(", ")}</td></tr>)}
      </tbody></table></div>
        {!!s.shadow_intents.length && <p className="muted">Shadow (evaluated, not funded, never executed): {Array.from(new Set(s.shadow_intents.map(i => i.strategy_id))).map(k => `${k} wants ${pct((s.shadow_intents.find(i => i.strategy_id === k)?.target_weight_bps || 0) / 10000)}`).join("; ")}</p>}
      </section>

      <section className="operational-grid"><div><h2>Strategy sleeves</h2><div className="table-scroll"><table><thead><tr><th>Sleeve</th><th>Capital</th><th>Equity</th><th>P&L</th><th>Fees</th><th>Fills</th><th>Holdings</th></tr></thead><tbody>
        {Object.entries(s.sleeves).map(([k, sl]) => <tr key={k}><td>{k}</td><td>{money(sl.capital)}</td><td>{money(sl.equity)}</td><td className={sl.pnl < 0 ? "neg" : "pos"}>{money(sl.pnl)}</td><td>{money(sl.fees)}</td><td>{sl.fills}</td><td className="muted">{Object.entries(sl.positions).map(([p, x]) => `${p} ${x.qty}`).join(", ") || "cash"}</td></tr>)}
        <tr><td>unallocated</td><td colSpan={6}>{money(s.unallocated_cash)}</td></tr>
      </tbody></table></div><p className="muted">Sleeves add up exactly to the account: lots, cash, fees and total profit.</p></div>
        <div><h2>Working orders and fills</h2><div className="table-scroll"><table><thead><tr><th>Order</th><th>Side</th><th>Remaining</th><th>Limit</th><th>Venue</th></tr></thead><tbody>
          {s.working_orders.map(o => <tr key={o.order_id}><td className="mono">{o.order_id}</td><td>{o.side}</td><td>{o.remaining}</td><td>{money(o.limit_px)}</td><td>{o.venue}</td></tr>)}
        </tbody></table></div>{!s.working_orders.length && <p className="muted">No working orders.</p>}
          <p className="muted">Fills by venue: {Object.entries(s.fills_by_venue).map(([k, n]) => `${k}: ${n}`).join(", ") || "none yet"}. Paper fills are simulated and never shown as exchange fills.</p></div></section>

      <section className="operational-grid"><div><h2>Strategy versions and promotion</h2><div className="table-scroll"><table><thead><tr><th>Sleeve</th><th>Family</th><th>Version</th><th>Capital</th></tr></thead><tbody>
        {s.strategies.map(st => <tr key={st.strategy_id}><td>{st.strategy_id}</td><td>{st.family}</td><td className="mono">{st.version}</td><td>{money(st.capital)}</td></tr>)}
      </tbody></table></div>
        <div className="table-scroll"><table><thead><tr><th>When</th><th>Event</th><th>Role</th><th>Version</th><th>Detail</th></tr></thead><tbody>
          {s.registry_history.slice().reverse().map((h, i) => <tr key={i}><td className="muted">{stamp(h.at)}</td><td>{h.event}</td><td>{h.role || "—"}</td><td className="mono">{h.version || "—"}</td><td className="muted">{h.weight !== undefined ? `weight ${pct(h.weight)}` : (h.reasons || []).join(", ")}</td></tr>)}
        </tbody></table></div></div>
        <div><h2><Gauge size={17} /> Latency and resources</h2><div className="table-scroll"><table><thead><tr><th>Stage</th><th>p50 / p95 / p99, microseconds</th></tr></thead><tbody>
          {Object.entries(s.timings).map(([k, l]) => <tr key={k}><td>{k}</td><td className="mono">{us(l)}</td></tr>)}
          <tr><td>ledger round trip (HTTP + worker + DB)</td><td className="mono">{us(s.ledger_round_trip)}</td></tr>
        </tbody></table></div><p className="muted">Gateway: {Object.entries(s.gateway).map(([k, n]) => `${k} ${n}`).join(", ")}. Ledger calls {s.ledger_calls}.</p></div></section>

      <section><h2><Layers size={17} /> Order book depth</h2><p className="muted">{Object.values(s.depth)[0]?.label}</p><div className="coord-depth">
        {Object.entries(s.depth).map(([k, b]) => <div key={k} className="card"><b>{k}</b><table><tbody>
          {b.asks.slice().reverse().map((a, i) => <tr key={"a" + i}><td className="neg">{money(a.px)}</td><td>{a.qty}</td></tr>)}
          {b.bids.map((a, i) => <tr key={"b" + i}><td className="pos">{money(a.px)}</td><td>{a.qty}</td></tr>)}
        </tbody></table></div>)}</div></section>
    </>}
    <EvaluationPanel e={v.evaluation} />
    {v.summary && <JsonView label="Session summary" value={v.summary} />}
  </>;
}

function EvaluationPanel({ e }: { e: Evaluation | null }) {
  if (!e) return null;
  return <section className="card"><h2>Does coordination beat buy-and-hold?</h2>
    {e.summary && <p>Across {e.summary.markets} fresh synthetic markets nobody tuned on, the coordinated portfolio's held-out return minus the passive baseline averaged <b className={e.summary.mean_excess < 0 ? "neg" : "pos"}>{pct(e.summary.mean_excess)}</b> (standard error {e.summary.standard_error === null ? "n/a" : pct(e.summary.standard_error)}). It was ahead in {e.summary.ahead_in}, equal in {e.summary.equal_in} (nothing funded, so it held the baseline), and behind in {e.summary.markets - e.summary.ahead_in - e.summary.equal_in}.</p>}
    {(e.datasets || []).map(ds => <div key={ds.dataset_id}><h3>{ds.dataset_id} <span className="muted">{ds.holdout_untouched ? "held-out evaluated once" : `held-out viewed ${ds.holdout_views} times: not untouched`}</span></h3>
      <div className="table-scroll"><table><thead><tr><th>Portfolio</th><th>Return</th><th>Max drawdown</th><th>Sharpe</th><th>Turnover</th></tr></thead><tbody>
        {Object.entries(ds.rows).map(([k, r]) => <tr key={k}><td>{r.label}{r.funded !== undefined ? (r.funded ? ` (funded ${pct(r.weight || 0)})` : " (not funded)") : ""}</td><td className={r.net_return < 0 ? "neg" : "pos"}>{pct(r.net_return)}</td><td>{pct(r.max_drawdown ?? 0)}</td><td>{r.sharpe}</td><td>{r.turnover_x_capital}x</td></tr>)}
      </tbody></table></div><ul className="muted">{ds.critic.map((c, i) => <li key={i}>{c}</li>)}</ul></div>)}
  </section>;
}
