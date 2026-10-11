"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { Download, Play, Square, Brain, ShieldAlert, CheckCircle2, Zap, ArrowUpRight, Radio, Flag, CircleDollarSign } from "lucide-react";
import { Analytics, CoordinatedContext, CoordinatedReport, Dataset, Job, Manifest, State, Tournament, View, errorText, familyName, id, money, pct, request, stamp, url } from "./model";
import { useCommand, useResource } from "./data";
import { Chart, Empty, JsonView, Metric, Notice } from "./ui";
import { ruleText } from "./help";
import { buildSimulationExport, downloadJson } from "./simulation-export";

/* ------------------------------------------------------------------ live ledger (whole session, not just the tail) */

type Raw = { seq: number; type: string; ts?: number; bar_ts?: number; symbol?: string; agent_id?: string; side?: string; qty?: number; requested_qty?: number;
  outcome?: string; rules?: string[]; px?: number; fee?: number; slippage?: number; order_id?: string; reason?: string; capital?: number; initial_cash?: number };
type Decision = Raw & { at: number };
type AgentStat = { approved: number; reduced: number; rejected: number; fills: number; last?: Decision };
type Ledger = { status: string; decisions: Decision[]; bars: number; steps: number; simTs: number; firstTs: number; orders: number; approved: number; reduced: number;
  rejected: number; fills: number; rules: Record<string, number>; agents: Record<string, AgentStat>; port: { seq: number; digest: string; equity: number; state: State } | null;
  curve: [number, number][] };
const MAX_DECISIONS = 6000;
const blank = (status: string): Ledger => ({ status, decisions: [], bars: 0, steps: 0, simTs: 0, firstTs: 0, orders: 0, approved: 0, reduced: 0, rejected: 0, fills: 0, rules: {}, agents: {}, port: null, curve: [] });

/** Streams every ledger event of one portfolio from sequence 0 and keeps the decisions; price bars are only counted. */
function useLedger(pid: string): Ledger {
  const [snap, setSnap] = useState<Ledger>(() => blank("connecting"));
  useEffect(() => {
    let active = true, socket: WebSocket | null = null, timer: ReturnType<typeof setTimeout> | undefined, last = 0;
    const acc = blank("connecting"), placed = new Map<string, Raw>();
    const publish = (status?: string) => { if (!active) return; if (status) acc.status = status; setSnap({ ...acc, decisions: [...acc.decisions], rules: { ...acc.rules }, agents: { ...acc.agents }, curve: [...acc.curve] }); };
    const absorb = (events: Raw[]) => {
      for (const e of events) {
        if (!Number.isInteger(e.seq) || typeof e.type !== "string" || e.seq <= last) continue;
        last = e.seq;
        if (e.type === "BarApplied") { acc.bars++; if (e.ts && e.ts !== acc.simTs) { acc.steps++; acc.simTs = e.ts; if (!acc.firstTs) acc.firstTs = e.ts; } continue; }
        const d: Decision = { ...e, at: acc.simTs };
        if (e.order_id) {
          if (e.type === "OrderAccepted" || e.type === "OrderRejected") placed.set(e.order_id, e);
          else { const o = placed.get(e.order_id); if (o) { d.agent_id ??= o.agent_id; d.symbol ??= o.symbol; d.side ??= o.side; } }
        }
        if (e.type === "OrderClosed" && e.reason === "filled") continue;
        const stat = d.agent_id ? (acc.agents[d.agent_id] ??= { approved: 0, reduced: 0, rejected: 0, fills: 0 }) : undefined;
        if (e.type === "OrderAccepted") { acc.orders++; if (e.outcome === "REDUCE") { acc.reduced++; if (stat) stat.reduced++; } else { acc.approved++; if (stat) stat.approved++; } }
        else if (e.type === "OrderRejected") { acc.orders++; acc.rejected++; if (stat) stat.rejected++; }
        else if (e.type === "OrderFilled") { acc.fills++; if (stat) stat.fills++; }
        if (e.type === "OrderAccepted" || e.type === "OrderRejected") for (const r of e.rules || []) acc.rules[r] = (acc.rules[r] || 0) + 1;
        if (stat && e.type.startsWith("Order")) stat.last = d;
        acc.decisions.push(d);
      }
      if (acc.decisions.length > MAX_DECISIONS) acc.decisions.splice(0, acc.decisions.length - MAX_DECISIONS);
    };
    const connect = () => {
      if (!active) return;
      const u = new URL(url("/v1/stream", { portfolio_id: pid, after_seq: last })); u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(u);
      socket.onopen = () => publish("connected");
      socket.onmessage = m => {
        try {
          const msg = JSON.parse(m.data);
          if (msg.type === "events" && Array.isArray(msg.events)) { absorb(msg.events); publish("connected"); }
          else if (msg.type === "portfolio" && msg.portfolio) {
            acc.port = msg.portfolio;
            // live equity point, timed by the newest bar inside this same snapshot so value and market time agree
            const bars = Object.values((msg.portfolio.state?.last_bar || {}) as Record<string, [number, number]>), ts = Math.max(0, ...bars.map(b => b[0]));
            if (ts && Number.isFinite(msg.portfolio.equity)) { if (acc.curve.at(-1)?.[0] === ts) acc.curve[acc.curve.length - 1] = [ts, msg.portfolio.equity]; else acc.curve.push([ts, msg.portfolio.equity]); }
            publish();
          }
        } catch { publish("invalid stream response"); }
      };
      socket.onclose = () => { if (active) { publish("reconnecting"); timer = setTimeout(connect, 2000); } };
    };
    connect();
    return () => { active = false; clearTimeout(timer); socket?.close(); };
  }, [pid]);
  return snap;
}

/** Agent messages for one portfolio, fetched incrementally: each poll asks only for ids newer than the last seen. */
function useMessages(pid: string, enabled: boolean, running: boolean) {
  const [state, setState] = useState<{ data: Message[]; error?: string }>({ data: [] });
  useEffect(() => {
    if (!enabled) return;
    let active = true, last = 0, fresh = true, timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const load = async () => {
      try {
        for (;;) {
          const rows = await request<Message[]>("/v1/agent-messages", { portfolio_id: pid, after_id: last, limit: 1000 }, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]) });
          if (!active) return;
          if (rows.length) last = rows[rows.length - 1].id;
          if (rows.length || fresh) { const first = fresh; fresh = false; setState(old => ({ data: first ? rows : [...old.data, ...rows] })); }
          if (rows.length < 1000) break;
        }
      } catch (e) { if (active) setState(old => ({ ...old, error: errorText(e) })); }
      finally { if (active && running) timer = setTimeout(load, 1500); }
    };
    void load();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [pid, enabled, running]);
  return state;
}

/* ------------------------------------------------------------------ helpers */

type Message = { id: number; agent_id: string; role: string; content: Record<string, unknown>; created_at: string; portfolio_id: string | null };
type Spec = { strategy_id: string; agent_id?: string; family?: string; universe: string[]; params?: Record<string, number> };
type Kind = "thinking" | "order" | "blocked" | "fill" | "system";
type FeedItem = { key: string; n: number; at: number; rank: number; kind: Kind; agent?: string; title: string; detail?: string; quote?: string; outcome?: string; tag?: string; raw: unknown };
type Target = { target_bps?: number; previous_bps?: number | null; reading?: string };

const obj = (v: unknown) => v && typeof v === "object" ? v as Record<string, unknown> : {};
const str = (v: unknown) => typeof v === "string" ? v : "";
const num = (v: unknown) => typeof v === "number" && Number.isFinite(v) ? v : undefined;
const nice = (agent?: string) => (agent || "system").replaceAll("_", " ");
const day = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
const signedMoney = (n: number, currency: string) => `${n >= 0 ? "+" : "−"}${money(Math.abs(n), currency)}`;
const END = Number.MAX_SAFE_INTEGER;
const targetText = (bps?: number) => !bps ? "out" : `${Math.round(bps / 100)}% in`;
/** One line per symbol of a recorded rule reading: where the rule stands and the indicator values behind it. */
const signalLines = (symbols: unknown) => Object.entries(obj(symbols)).map(([sym, v]) => {
  const t = obj(v) as Target, moved = t.previous_bps !== null && t.previous_bps !== undefined;
  return `${sym} ${moved ? `${targetText(t.previous_bps!)} → ${targetText(t.target_bps)}` : targetText(t.target_bps)}${t.reading ? `: ${t.reading}` : ""}`;
});
const FINISHED = ["completed", "stopped", "failed", "interrupted"];

/** What each approved strategy type does, in plain words. The trading rule is fixed code; the AI only chose the type and its settings. */
const HOW: Record<string, string> = {
  sma_crossover: "Holds a stock while its short-term average price is above its long-term average, and steps out when it drops below.",
  breakout: "Buys when a price jumps above its recent high and sells if it falls back below its recent low.",
  zscore_reversion: "Buys when a price has fallen unusually far below its recent average and sells once it recovers.",
  vol_target: "Holds a stock only while its price is above its longer-term average, and sizes the holding to keep risk steady: more when calm, less when wild.",
  rsi_reversion: "Buys after a stretch of heavy selling and sells once the price has bounced back.",
  ts_momentum: "Holds a stock only while it is up over its lookback window, and steps aside when it isn't.",
  regime_reversion: "Holds by default. In a range-bound market it steps aside when prices are stretched above average and returns when they come back.",
  buy_hold: "Passive core: buys every symbol equally and simply holds.",
};

function specsOf(manifest: Manifest | null): Spec[] {
  if (!manifest) return [];
  const entries = [...manifest.fitted.map(f => f.spec as Spec), ...(manifest.core ? [manifest.core.spec as Spec] : [])];
  return entries.map(s => ({ ...s, agent_id: s.agent_id || "core", family: s.family || manifest.ranked.find(r => r.strategy_id === s.strategy_id)?.family || (manifest.core?.spec.strategy_id === s.strategy_id ? "buy_hold" : undefined) }));
}

/** Strategies (not the passive core) that research gave a budget to. */
const fundedCount = (m: Manifest) => Object.entries(m.allocations.by_strategy).filter(([sid, a]) => a.capital_micros > 0 && sid !== m.core?.spec.strategy_id).length;

function maxDrawdown(values: number[]) {
  let peak = 0, worst = 0;
  for (const v of values) { if (v > peak) peak = v; if (peak > 0) worst = Math.max(worst, (peak - v) / peak); }
  return worst;
}

/* ------------------------------------------------------------------ view */

export function SimulationView({ pid, manifest, dataset, datasets, runs, onSelect, go }: {
  pid: string; manifest: Manifest | null; dataset?: Dataset; datasets: Dataset[]; runs: Tournament[]; onSelect: (p: string) => void; go: (v: View) => void;
}) {
  const ledger = useLedger(pid);
  const pidJobs = useResource<Job[]>("/v1/jobs", { portfolio_id: pid, include_cleared: "true" }, 2000);
  const job = pidJobs.data?.find(j => ["session", "coordinated"].includes(j.kind)) as (Job & { config?: { steps?: number; speed?: number; crash_at?: number | null } }) | undefined;
  const running = !!job && !FINISHED.includes(job.status), finished = !!job && FINISHED.includes(job.status);
  const exists = !!ledger.port || !!job;
  // polled while the replay runs, then fetched once more when it ends so the final numbers are complete
  const settled = finished ? 1 : 0;
  // Kept light on purpose: the API serves the replay itself. The recorded curve is read once and extended from the
  // stream; per-agent profit (a full ledger replay on the server) refreshes only every 30 seconds while running.
  const equity = useResource<{ ts: string; equity: number }[]>(exists ? "/v1/equity" : null, { portfolio_id: pid, limit: 12000 }, 0, settled);
  const analytics = useResource<Analytics>(exists ? "/v1/analytics/history" : null, { portfolio_id: pid }, running ? 30000 : 0, settled);
  const messages = useMessages(pid, exists, running);
  const coordinated = job?.kind === "coordinated";
  const report = useResource<CoordinatedReport>(coordinated ? "/v1/coordinated/report" : null,
    { portfolio_id: pid }, running ? 15000 : 0, settled, true);
  const context = report.data?.context || messages.data.find(m => m.role === "coordinator_start")?.content.context as CoordinatedContext | undefined;
  const sleeves = report.data?.final_attribution || report.data?.steps.at(-1)?.sleeves || {};
  const stop = useCommand();
  const [exporting, setExporting] = useState(false), [exportError, setExportError] = useState("");
  async function download() {
    setExporting(true); setExportError("");
    try { downloadJson(`simulation-${pid}.json`, await buildSimulationExport(pid, manifest, dataset, job, HOW)); }
    catch (e) { setExportError(`Download failed: ${errorText(e)}`); }
    finally { setExporting(false); }
  }
  const [filter, setFilter] = useState<"all" | Kind>("all"), [agentFilter, setAgentFilter] = useState(""), [newest, setNewest] = useState(true), [shown, setShown] = useState(120);

  const lots = dataset?.lots_per_base_unit || 1, currency = dataset?.quote_currency || "USD";
  const specs = useMemo(() => coordinated && context ? context.agents.map(a => ({ ...a })) : specsOf(manifest), [manifest, coordinated, context]);
  const familyOf = useMemo(() => Object.fromEntries(specs.map(s => [s.agent_id!, s.family])), [specs]);
  const st = ledger.port?.state;
  const q = (n?: number) => n === undefined ? "?" : `${n / lots}`;

  const feed = useMemo(() => {
    const items: FeedItem[] = [];
    const why = (d: Decision) => {
      if (d.order_id?.includes(".crisis")) return "Part of its response to the crash.";
      const family = familyOf[d.agent_id || ""];
      if (!family) return "";
      if (family === "buy_hold") return d.side === "BUY" ? "The passive core is building its equal-weight holding." : "The passive core is trimming its holding.";
      return `Its ${familyName(family)} rule now calls for a ${d.side === "BUY" ? "bigger" : "smaller"} ${d.symbol} position.`;
    };
    for (const d of ledger.decisions) {
      const base = { key: `e${d.seq}`, n: d.seq, at: d.at, rank: 1, agent: d.agent_id, raw: d };
      const rules = (d.rules || []).map(ruleText).join(", ");
      if (d.type === "OrderAccepted") {
        const reduced = d.outcome === "REDUCE";
        items.push({ ...base, kind: "order", outcome: reduced ? "REDUCE" : "APPROVE", title: `${nice(d.agent_id)} wants to ${d.side === "BUY" ? "buy" : "sell"} ${q(d.requested_qty ?? d.qty)} ${d.symbol}`,
          detail: `${why(d)} ${reduced ? `The risk engine shrank it to ${q(d.qty)}: ${rules || "limit reached"}.` : "The risk engine approved it as asked."}`.trim() });
      } else if (d.type === "OrderRejected") {
        items.push({ ...base, kind: "blocked", outcome: "REJECT", title: `${nice(d.agent_id)} wanted to ${d.side === "BUY" ? "buy" : "sell"} ${q(d.requested_qty)} ${d.symbol}`,
          detail: `${why(d)} The risk engine blocked it: ${rules || "rule violated"}.`.trim() });
      } else if (d.type === "OrderFilled") {
        items.push({ ...base, kind: "fill", title: `${nice(d.agent_id)} ${d.side === "BUY" ? "bought" : "sold"} ${q(d.qty)} ${d.symbol} at ${money((d.px || 0) * lots, currency)}`,
          detail: `Trade done on the next price bar. Fee ${money(d.fee, currency)}${d.slippage ? `, slippage ${money(d.slippage, currency)}` : ""}.` });
      } else if (d.type === "OrderClosed") {
        items.push({ ...base, kind: "system", title: `Order ${d.reason || "closed"}: ${nice(d.agent_id)} ${d.side || ""} ${d.symbol || ""}`.trim(), detail: "It did not fill completely; anything set aside for it was released." });
      } else if (d.type === "AllocationSet") {
        items.push({ ...base, kind: "system", title: `Budget set: ${nice(d.agent_id)} may use ${money(d.capital, currency)}` });
      } else if (d.type === "PortfolioOpened") {
        items.push({ ...base, kind: "system", title: `Account opened with ${money(d.initial_cash, currency)} of pretend money` });
      } else {
        items.push({ ...base, kind: "system", title: d.type.replace(/([a-z])([A-Z])/g, "$1 $2") });
      }
    }
    let crashAt = 0;
    for (const m of messages.data || []) {
      if (m.portfolio_id !== pid) continue;
      const c = m.content, base = { key: `m${m.id}`, n: m.id, rank: 0, raw: c };
      if (m.role === "coordinator_start") {
        const ctx = c.context as CoordinatedContext;
        items.push({ ...base, at: 0, kind: "system", title: "Coordinated paper CLOB simulation started",
          detail: `${ctx.dataset.dataset_id} · ${ctx.policy_version} · ${ctx.code_version}. ${ctx.depth}. Fixed strategy rules execute; AI remarks are commentary.` });
      } else if (m.role === "coordinator_decision") {
        items.push({ ...base, at: num(c.ts) || 0, kind: "order", title: `Coordinator: ${str(c.instrument)}`,
          detail: `${str(c.explanation)} ${JSON.stringify(c.intents)}` });
      } else if (m.role === "coordinator_summary") {
        items.push({ ...base, at: END, kind: "system", outcome: "DONE", title: "Coordinated simulation finished",
          detail: `${num(c.cycles)} completed cycles, ${num(c.fills)} fills; ${num(c.reconciliation_breaks)} reconciliation breaks. Baseline uses these same completed bars.` });
      } else if (m.role === "session_start") {
        const crash = num(c.crash_start_step);
        items.push({ ...base, at: 0, kind: "system", title: `Simulation started: replaying ${num(c.steps) ?? "the"} market steps`, detail: crash === undefined ? "No crash is scheduled." : `A scripted crash is scheduled at step ${crash}.` });
      } else if (m.role === "market_crash") {
        crashAt = Date.parse(str(c.ts)) / 1000 || crashAt;
        items.push({ ...base, at: crashAt, kind: "system", outcome: "CRASH", title: "Scripted market crash begins", detail: `${str(c.overlay)}. This is injected on purpose to stress the agents and the risk engine; it is not a real event.` });
      } else if (m.role === "crisis_response") {
        const decision = obj(c.decision), action = str(decision.action), fraction = num(decision.fraction);
        const orders = Array.isArray(c.orders) ? c.orders.map(obj) : [];
        const failure = /^fallback \((.+)\)$/.exec(str(decision.source))?.[1];
        const source = str(decision.source) === "llm" ? `AI model ${str(obj(decision.llm).model) || ""}`.trim() : failure ? `the scripted fallback, because the AI model call failed (${failure})` : "the scripted fallback (no AI model configured)";
        items.push({ ...base, at: crashAt, kind: "thinking", agent: m.agent_id,
          title: `${nice(m.agent_id)} decides to ${action === "buy_dip" ? "buy the dip" : action === "reduce" ? "cut its holdings" : "hold steady"}${fraction && action !== "hold" ? ` (${Math.round(fraction * 100)}%)` : ""}`,
          quote: str(decision.rationale),
          detail: `Decided by ${source}. ${orders.length ? `Resulting orders: ${orders.map(o => `${str(o.side)} ${q(num(o.qty))} ${str(o.symbol)} → ${str(o.outcome) || str(o.status)}`).join("; ")}.` : "No orders resulted."}` });
      } else if (m.role === "thought") {
        items.push({ ...base, at: num(c.ts) || 0, kind: "thinking", agent: m.agent_id, tag: "AI", title: `${nice(m.agent_id)} thinks`, quote: str(c.thought),
          detail: `AI remark by ${str(obj(c.llm).model) || "the configured model"} at step ${num(c.step) ?? "?"}${str(c.stance) ? `, stance: ${str(c.stance)}` : ""}. It reads the recorded facts and cannot place orders; its fixed rule does.` });
      } else if (m.role === "signal") {
        const lines = signalLines(c.symbols), symbols = Object.entries(obj(c.symbols)), first = obj(symbols[0]?.[1]) as Target;
        const verb = !first.previous_bps && first.target_bps ? "switches into" : !first.target_bps ? "steps out of" : (first.target_bps || 0) > (first.previous_bps || 0) ? "raises its target for" : "lowers its target for";
        items.push({ ...base, at: num(c.ts) || 0, kind: "thinking", agent: m.agent_id, tag: "RULE",
          title: c.opening === true ? `${nice(m.agent_id)} sizes up the market` : symbols.length === 1 ? `${nice(m.agent_id)}'s rule ${verb} ${symbols[0][0]}` : `${nice(m.agent_id)}'s rule changes ${symbols.length} targets`,
          detail: `${lines.join(". ")}.${c.funded === false ? " It has no budget, so this is watch-only." : ""}` });
      } else if (m.role === "thinking_unavailable") {
        items.push({ ...base, at: num(c.ts) || 0, kind: "system", title: "AI remarks are unavailable for this run",
          detail: str(c.reason) === "no AI model configured" ? "No AI model is configured, so only the rule readings are shown." : `The AI model call failed (${str(c.reason)}), so only the rule readings are shown.` });
      } else if (m.role === "session_summary") {
        items.push({ ...base, at: END, kind: "system", outcome: "DONE", title: "Simulation finished", detail: `${num(c.steps) ?? "?"} steps replayed, ${num(c.orders) ?? "?"} order requests sent${num(c.duplicates) ? ` (including ${num(c.duplicates)} deliberate duplicate retries, which had no effect)` : ""}. Ledger replay ${obj(c.verify).match === true ? "matches" : "did not match"} the stored account.` });
      }
    }
    return items.sort((a, b) => a.at - b.at || a.rank - b.rank || a.n - b.n);
    // q/lots/currency derive from dataset; familyOf from the manifest
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ledger.decisions, messages.data, pid, lots, currency, familyOf]);

  const visibleFeed = useMemo(() => {
    const list = feed.filter(i => (filter === "all" || i.kind === filter) && (!agentFilter || i.agent === agentFilter));
    return newest ? [...list].reverse() : list;
  }, [feed, filter, agentFilter, newest]);

  /** Latest AI remark and latest rule reading per agent, for the live "thinking now" panel. */
  const now = useMemo(() => {
    const out: Record<string, { thought?: Message; signal?: Message }> = {};
    for (const m of messages.data || []) {
      if (m.portfolio_id !== pid || (m.role !== "thought" && m.role !== "signal")) continue;
      (out[m.agent_id] ??= {})[m.role] = m;
    }
    return out;
  }, [messages.data, pid]);
  const curve = useMemo(() => {
    const recorded = [...(equity.data || [])].reverse().map(p => [Date.parse(p.ts) / 1000, p.equity] as [number, number]);
    if (finished && recorded.length) return recorded;
    const merged = new Map<number, number>(recorded);
    for (const [ts, value] of ledger.curve) merged.set(ts, value);
    return [...merged.entries()].sort((a, b) => a[0] - b[0]);
  }, [equity.data, ledger.curve, finished]);
  const baseline = analytics.data?.benchmark.baseline;
  const lastTs = curve.at(-1)?.[0] || ledger.simTs, firstTs = curve[0]?.[0] || ledger.firstTs;
  const baseCurve = useMemo(() => (baseline || []).filter(p => p.ts >= firstTs && p.ts <= lastTs).map(p => [p.ts, p.equity] as [number, number]), [baseline, firstTs, lastTs]);
  // the crash window comes from the session's own record of when it injected it (four bars from that market time)
  const crashTs = Date.parse(str(messages.data.find(m => m.portfolio_id === pid && m.role === "market_crash")?.content.ts)) / 1000 || 0;
  const crashFrom = crashTs ? curve.findIndex(p => p[0] >= crashTs) : -1;
  const crash = crashTs ? { start: crashTs, end: crashFrom >= 0 ? curve[Math.min(crashFrom + 4, curve.length - 1)][0] : crashTs, label: "Simulated crash" } : undefined;
  const agentPnl = analytics.data?.points.at(-1)?.agents || {};

  const initial = st?.initial_cash || manifest?.policy.initial_cash_micros || 10_000_000_000;
  const equityNow = ledger.port?.equity;
  const ret = equityNow === undefined ? undefined : equityNow / initial - 1;
  const total = job?.total || (running ? undefined : ledger.steps) || undefined;
  const step = Math.max(job?.progress || 0, ledger.steps);
  const result = obj(job?.result);
  const baseRet = baseCurve.length ? baseCurve.at(-1)![1] / initial - 1 : undefined;
  const topRule = Object.entries(ledger.rules).sort((a, b) => b[1] - a[1])[0];
  // only agents that were given money or actually traded are worth ranking
  const ranked = Object.entries(agentPnl).filter(([a]) => (sleeves[a]?.capital || st?.allocations[a] || 0) > 0 || !!ledger.agents[a]?.fills).sort((a, b) => b[1].pnl - a[1].pnl);
  const agentsInFeed = [...new Set(feed.map(i => i.agent).filter(Boolean))] as string[];
  const icon = (i: FeedItem) => i.outcome === "CRASH" ? <Zap size={15} /> : i.outcome === "DONE" ? <Flag size={15} /> : i.kind === "thinking" ? <Brain size={15} /> : i.kind === "blocked" ? <ShieldAlert size={15} /> : i.kind === "fill" ? <CircleDollarSign size={15} /> : i.kind === "order" ? <CheckCircle2 size={15} /> : <Radio size={15} />;

  return <div className="sim">
    <div className="section-title"><h1>Simulation</h1><span className="badge">Historical replay · pretend money</span></div>
    <Launcher pid={pid} manifest={manifest} datasets={datasets} runs={runs} onSelect={onSelect} open={!exists} />

    {!exists ? <section><Empty>{ledger.status === "connecting" || pidJobs.loading ? "Loading simulation…" : `Portfolio ${pid} has no simulation yet. Pick a research run above and press Start simulation to watch it play from start to finish.`}</Empty></section> : <>
      <section className="sim-status">
        <div className="section-title"><div><h2>{pid}</h2><span className="muted">{coordinated ? `${context?.dataset.dataset_id || job?.dataset_id} · coordinated registry · ${context?.policy_version || "checking research"}` : manifest ? `Legacy session · Research ${manifest.run_id} · ${manifest.dataset.dataset_id}` : "Legacy recorded session"}</span></div>
          <div className="row">
            <span className={`badge ${job?.status === "failed" || job?.status === "interrupted" ? "bad" : running ? "good" : ""}`}>{running && <span className="live-dot" />}{job ? (job.status === "running" ? job.stage.replaceAll("_", " ") : job.status) : "recorded session"}</span>
            <button disabled={exporting} onClick={download} title="Everything recorded for this simulation as one JSON file: metrics, every step, agents, thinking, decisions and trades"><Download size={14} />{exporting ? "Preparing…" : "Download full history"}</button>
            {running && <button disabled={stop.pending || job!.stop_requested} onClick={() => stop.send(`/v1/jobs/${encodeURIComponent(job!.job_id)}/stop`, {})}><Square size={14} />{job!.stop_requested ? "Stopping…" : "Stop"}</button>}
          </div></div>
        <progress value={step} max={total || Math.max(step, 1)} />
        <div className="sim-clock"><span><b>Step {step.toLocaleString()}</b>{total ? ` of ${total.toLocaleString()}` : ""}</span>
          <span>{ledger.simTs ? <>Market time <b>{day(ledger.simTs)}</b>{ledger.firstTs ? <span className="muted"> · started {day(ledger.firstTs)}</span> : null}</> : "Waiting for the first price bar"}</span>
          <span className="muted">Stream {ledger.status} · {ledger.bars.toLocaleString()} price bars · ledger #{ledger.port?.seq ?? 0}</span></div>
        {job?.error && <Notice error>{job.error}</Notice>}{stop.error && <Notice error>{stop.error}</Notice>}{exportError && <Notice error>{exportError}</Notice>}
        {context && !context.agents.some(a => a.funded && a.agent_id !== "core") && <Notice>No active strategy passed the current promotion gates. The passive core trades; rejected challengers remain watch-only. This is the expected fallback, not evidence of beating buy and hold.</Notice>}
        {context && <p className="muted small">{context.evaluation_status}. Data {context.dataset.content_sha256.slice(0, 16)} · runtime {context.code_version} · source {context.source_sha256.slice(0, 16)}. {context.depth}.</p>}
        {job?.status === "queued" && <Notice>Queued: the local manager starts a dedicated execution worker for this portfolio, then the replay begins.</Notice>}
      </section>

      <div className="metrics">
        <Metric label="Account value" value={equityNow === undefined ? "Not opened" : money(equityNow, currency)} />
        <Metric label="Return so far" value={ret === undefined ? "—" : <span className={ret < 0 ? "neg" : "pos"}>{`${ret >= 0 ? "+" : ""}${(ret * 100).toFixed(2)}%`}</span>} />
        <Metric label="Cash" value={st ? money(st.cash, currency) : "—"} />
        <Metric label="Invested" value={st && equityNow ? pct(st.market_value / equityNow) : "—"} />
        <Metric label="Orders approved / shrunk / blocked" value={`${ledger.approved} / ${ledger.reduced} / ${ledger.rejected}`} />
        <Metric label="Fees + slippage" value={st ? money(st.fees_paid + st.slippage_cost, currency) : "—"} />
      </div>

      {finished && <section className="sim-results">
        <div className="section-title"><h2>Results</h2><span className={`badge ${job!.status === "completed" ? "good" : "warn"}`}>{job!.status === "completed" ? "Ran start to finish" : `Ended early: ${job!.status}`}</span></div>
        <ul className="sim-story">
          <li>The account {ret !== undefined && ret < 0 ? "ended down at" : "ended at"} <b>{money(equityNow, currency)}</b>{ret !== undefined && <> (<span className={ret < 0 ? "neg" : "pos"}>{`${ret >= 0 ? "+" : ""}${(ret * 100).toFixed(2)}%`}</span>)</>} after {step.toLocaleString()} market steps{firstTs && lastTs ? `, ${day(firstTs)} to ${day(lastTs)}` : ""}.</li>
          {curve.length > 1 && <li>Its worst drop from a high point was <b>{pct(maxDrawdown(curve.map(p => p[1])))}</b>.</li>}
          {baseRet !== undefined && ret !== undefined && <li>Simply buying every stock equally and holding returned <b>{`${baseRet >= 0 ? "+" : ""}${(baseRet * 100).toFixed(2)}%`}</b> over the same dates, so the agents finished <b className={ret - baseRet < 0 ? "neg" : "pos"}>{Math.abs((ret - baseRet) * 100).toFixed(2)} points {ret - baseRet < 0 ? "behind" : "ahead"}</b>.{crash ? " The buy-and-hold line does not include the scripted crash." : ""}</li>}
          {ranked.length > 1 && <li>Best agent: <b>{nice(ranked[0][0])}</b> ({signedMoney(ranked[0][1].pnl, currency)}). Weakest: <b>{nice(ranked.at(-1)![0])}</b> ({signedMoney(ranked.at(-1)![1].pnl, currency)}).</li>}
          <li>Agents sent <b>{ledger.orders}</b> orders: {ledger.approved} approved, {ledger.reduced} shrunk and {ledger.rejected} blocked by the risk engine{topRule ? `. The most common reason was "${ruleText(topRule[0])}" (${topRule[1]} times)` : ""}. {ledger.fills} trades actually filled.</li>
          {obj(result.verify).match !== undefined && <li>Books check: replaying all {num(obj(result.verify).events)?.toLocaleString() ?? ""} ledger records from scratch <b className={obj(result.verify).match === true ? "pos" : "neg"}>{obj(result.verify).match === true ? "reproduces exactly the same account" : "does NOT match the stored account"}</b>.</li>}
        </ul>
        <div className="row"><button className="cta-pill" onClick={() => go("portfolio")}>Open portfolio <ArrowUpRight size={15} /></button><button onClick={() => go("orders")}>All orders</button><button onClick={() => go("analytics")}>Analytics</button><button onClick={() => go("audit")}>Audit the ledger</button></div>
        <JsonView label="Recorded session summary" value={job!.result} />
      </section>}

      <h2 className="sim-h">The agents</h2>
      {!specs.length ? <Notice>No research record is associated with this portfolio, so agent theses are unavailable. Decisions still appear below.</Notice> : <div className="sim-agents">{specs.map(s => {
        const agent = s.agent_id!, coordinatedAgent = context?.agents.find(a => a.agent_id === agent);
        const proposal = coordinated ? undefined : manifest?.proposals.find(p => p.agent_id === agent), rank = coordinated ? undefined : manifest?.ranked.find(r => r.strategy_id === s.strategy_id);
        const stat = ledger.agents[agent], budget = coordinatedAgent?.capital ?? st?.allocations[agent], pnl = agentPnl[agent]?.pnl;
        const holdings = coordinated ? Object.entries(sleeves[agent]?.positions || {}).map(([sym, p]) => `${q(p.qty)} ${sym}`) : Object.entries(st?.positions || {}).filter(([k, p]) => k.split("|")[0] === agent && p.qty > 0).map(([k, p]) => `${q(p.qty)} ${k.split("|")[1]}`);
        const last = stat?.last, thought = now[agent]?.thought, signal = now[agent]?.signal;
        return <article className="sim-agent" key={agent}>
          <div className="sim-agent-head"><div><span className="eyebrow">{nice(agent)}</span><h3>{familyName(s.family || "strategy")}</h3></div>
            <span className={`badge ${budget === 0 ? "warn" : proposal?.source === "llm" ? "good" : ""}`}>{s.family === "buy_hold" ? "passive" : budget === 0 ? "no budget · watching" : proposal?.source === "llm" ? "AI proposed" : proposal?.source || "no proposal"}</span></div>
          {s.family !== "buy_hold" && <div className={`sim-now ${running ? "live" : ""}`}>
            <span className="eyebrow">{running && <span className="live-dot" />}<Brain size={13} /> {running ? "Thinking now" : "Last thinking"}</span>
            {thought ? <blockquote>{str(thought.content.thought)}</blockquote> : <p className="muted small">{running ? "Waiting for its first AI remark…" : "No AI remark was recorded for this run."}</p>}
            {signal && <p className="small"><b>Rule reading:</b> {signalLines(signal.content.symbols).slice(0, 3).join(". ")}.</p>}
            {(thought || signal) && <span className="muted small">{day(num((thought || signal)!.content.ts) || 0)} · step {num((thought || signal)!.content.step) ?? "?"}{thought ? ` · ${str(obj(thought.content.llm).model)}` : ""}</span>}
          </div>}
          <p className="sim-how"><b>How it trades.</b> {HOW[s.family || ""] || "Fixed, approved rule chosen during research."}</p>
          {proposal && <p className="sim-thesis"><b>Research thesis.</b> {proposal.proposal.rationale}</p>}
          {coordinatedAgent && <><p className="sim-thesis"><b>Research thesis.</b> {coordinatedAgent.hypothesis}</p><p className="muted small">Frozen version {coordinatedAgent.version}. {coordinatedAgent.funded ? "Funded by the current registry." : `Not funded: ${coordinatedAgent.reasons.join("; ")}.`}</p></>}
          {rank && <p className="muted small">Judged on unseen data: {pct(rank.validation.net_return)} return, Sharpe {rank.validation.sharpe.toFixed(2)}.{rank.gates.length ? ` Not funded: ${rank.gates.join("; ")}.` : " Passed its checks."}</p>}
          <div className="sim-agent-stats"><span><small>Budget</small><b>{budget === undefined ? "—" : money(budget, currency)}</b></span>
            <span><small>Profit / loss</small><b className={pnl !== undefined && pnl < 0 ? "neg" : pnl ? "pos" : ""}>{pnl === undefined ? "—" : signedMoney(pnl, currency)}</b></span>
            <span><small>Holding</small><b>{holdings.length ? holdings.join(", ") : "nothing"}</b></span></div>
          <p className="small">{last ? <><b>Latest:</b> {last.type === "OrderFilled" ? `${last.side === "BUY" ? "bought" : "sold"} ${q(last.qty)} ${last.symbol}` : last.type === "OrderRejected" ? `blocked ${last.side} ${last.symbol} (${ruleText(last.rules?.[0] || "")})` : last.type === "OrderAccepted" ? `ordered ${last.side} ${q(last.qty)} ${last.symbol}` : last.type.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase()}{last.at ? ` · ${day(last.at)}` : ""}</> : <span className="muted">{budget === 0 ? "No budget, so it does not trade." : "No trades yet."}</span>}</p>
          {stat && stat.approved + stat.reduced + stat.rejected > 0 && <p className="muted small">{stat.approved} approved · {stat.reduced} shrunk · {stat.rejected} blocked · {stat.fills} fills</p>}
          {proposal && <JsonView label="Evidence and settings it chose" value={{ evidence: proposal.proposal.evidence, fitted_settings: s.params, universe: s.universe }} />}
        </article>;
      })}</div>}
      {manifest?.supervisor?.rationale && <p className="sim-supervisor"><b>Supervisor ({manifest.supervisor.source === "llm" ? "AI" : manifest.supervisor.source}) on how the money was split:</b> {manifest.supervisor.rationale}</p>}

      <section>
        <h2>Account value over market time</h2>
        {equity.error && <Notice error>{equity.error}</Notice>}
        <Chart currency={currency} bands={crash ? [crash] : []} series={[{ name: "Agents (this simulation)", color: "#28d49b", values: curve }, ...(baseCurve.length > 1 ? [{ name: "Buy & hold everything", color: "#5b8cff", values: baseCurve }] : [])]} />
        <p className="muted small">Dates are market time of the replayed data.{crash && baseCurve.length > 1 ? " The buy-and-hold line is the recorded research baseline and does not include the scripted crash." : ""}{analytics.data && !baseline ? ` Buy-and-hold comparison unavailable: ${analytics.data.benchmark.reason || "no baseline recorded for this research run"}.` : ""}</p>
      </section>

      <section className="sim-feed">
        <div className="section-title"><h2>Thinking &amp; decisions <span className="muted small">{feed.length.toLocaleString()} recorded</span></h2>
          <div className="row"><div className="tabs" aria-label="Feed filter">{([["all", "All"], ["thinking", "Thinking"], ["order", "Approved"], ["blocked", "Blocked"], ["fill", "Trades"], ["system", "System"]] as const).map(([k, label]) => <button key={k} className={filter === k ? "active" : ""} aria-pressed={filter === k} onClick={() => { setFilter(k); setShown(120); }}>{label}</button>)}</div>
            <select aria-label="Agent filter" value={agentFilter} onChange={e => { setAgentFilter(e.target.value); setShown(120); }}><option value="">All agents</option>{agentsInFeed.map(a => <option key={a} value={a}>{nice(a)}</option>)}</select>
            <button onClick={() => setNewest(!newest)}>{newest ? "Newest first" : "Oldest first"}</button></div></div>
        {messages.error && <Notice error>Agent messages unavailable: {messages.error}</Notice>}
        {!visibleFeed.length ? <Empty>{running ? "Decisions appear here as the agents trade." : "Nothing recorded for this filter."}</Empty> : <ol className="sim-timeline">{visibleFeed.slice(0, shown).map(i => <li key={i.key} className={`sim-item ${i.kind}`}>
          <span className="sim-icon">{icon(i)}</span>
          <div className="sim-body"><div className="sim-line"><b>{i.title}</b>{i.tag && <span className="badge">{i.tag}</span>}{i.outcome && !["CRASH", "DONE"].includes(i.outcome) && <span className={`decision ${i.outcome.toLowerCase()}`}>{i.outcome}</span>}</div>
            {i.quote && <blockquote>{i.quote}</blockquote>}
            {i.detail && <p>{i.detail}</p>}
            <span className="muted small">{i.at && i.at !== END ? day(i.at) : i.at === END ? "End of simulation" : "Before the first bar"}</span></div>
        </li>)}</ol>}
        {visibleFeed.length > shown && <button onClick={() => setShown(shown + 200)}>Show {Math.min(200, visibleFeed.length - shown)} more of {(visibleFeed.length - shown).toLocaleString()}</button>}
      </section>
    </>}
  </div>;
}

/* ------------------------------------------------------------------ launcher */

function Launcher({ pid, manifest, datasets, runs, onSelect, open }: { pid: string; manifest: Manifest | null; datasets: Dataset[]; runs: Tournament[]; onSelect: (p: string) => void; open: boolean }) {
  const usable = useMemo(() => datasets.filter(d => d.execution_usable && d.interval_s === 3600 && !d.synthetic && /alpaca|stock|equity|etf/i.test(d.source + d.dataset_id)), [datasets]);
  const synthetic = datasets.filter(d => d.execution_usable && d.interval_s === 3600 && d.synthetic);
  const [datasetId, setDatasetId] = useState(""), [length, setLength] = useState("full"), [duration, setDuration] = useState("max"), [error, setError] = useState("");
  const ds = [...usable, ...synthetic].find(d => d.dataset_id === datasetId) || usable.find(d => d.dataset_id === "alpaca-sip-1hour-2023-01-03-2026-09-30") || usable[0];
  const research = useResource<CoordinatedContext>(ds ? "/v1/coordinated/research" : null, { dataset_id: ds?.dataset_id }, 15000);
  const c = useCommand(), identity = useRef<{ signature: string; request_id: string; portfolio_id: string } | null>(null);
  const past = useResource<Job[]>("/v1/jobs", { include_cleared: "true" }, 15000);
  const sessions = (past.data || []).filter(j => ["session", "coordinated"].includes(j.kind) && j.portfolio_id).slice(0, 8);
  async function start() {
    if (!ds) { setError("No usable real hourly stock/ETF snapshot is available. Import one, or explicitly choose the labeled synthetic fixture."); return; }
    // steps is capped by the API at 10,000; the replay itself stops at the end of the held-out period
    const config = { kind: "coordinated", dataset_id: ds.dataset_id, steps: length === "full" ? 10000 : Number(length), speed: 0, crash_at: null, duration_s: duration === "max" ? null : Number(duration) };
    const signature = JSON.stringify(config);
    if (identity.current?.signature !== signature) identity.current = { signature, request_id: id("job"), portfolio_id: `sim-${Date.now().toString(36)}` };
    setError("");
    const r = await c.send("/v1/jobs", { ...config, request_id: identity.current.request_id, portfolio_id: identity.current.portfolio_id });
    if (r) { const p = identity.current.portfolio_id; identity.current = null; onSelect(p); }
  }
  const test = ds?.splits.test;
  return <details className="sim-launch" open={open || undefined}>
    <summary><Play size={15} /> New simulation</summary>
    <p className="muted">Replays hourly stock/ETF history through the current coordinated registry and paper CLOB with $10,000 in a fresh portfolio. Strategies share one market snapshot; opposing intents net before one aggregate order per symbol. Current-policy research runs first when needed.</p>
    <div className="form-grid">
      <label>Market data<select aria-label="Simulation market data" value={ds?.dataset_id || ""} onChange={e => setDatasetId(e.target.value)}>{!usable.length && <option value="">No real stock/ETF snapshot available</option>}{usable.map(d => <option key={d.dataset_id} value={d.dataset_id}>{d.dataset_id} · real historical prices</option>)}{synthetic.map(d => <option key={d.dataset_id} value={d.dataset_id}>{d.dataset_id} · SYNTHETIC fixture (explicit choice)</option>)}</select></label>
      <label>How much to replay<select aria-label="Simulation length" value={length} onChange={e => setLength(e.target.value)}><option value="full">Whole period, start to finish</option><option value="300">First 300 steps (quick look)</option><option value="100">First 100 steps</option></select></label>
      <label>Finish in<select aria-label="Simulation duration" value={duration} onChange={e => setDuration(e.target.value)}><option value="max">As fast as it can</option><option value="240">Spread over about 4 minutes</option></select></label>
    </div>
    {test && <p className="muted small">Period: {day(test[0])} to {day(test[1])} (exclusive end). Exploratory replay of previously inspected history. Runtime depends on API/worker speed; a duration target cannot accelerate slow execution.</p>}
    {research.data && <Notice>{research.data.current ? `${research.data.agents.filter(a => a.funded && a.agent_id !== "core").length} active strategies funded by ${research.data.policy_version}. Rejected challengers remain watch-only; residual capital follows the passive core.` : "Compatible research is missing. Starting will research on training/validation data before freezing allocations and replaying. Old tournament allocations will not be used."}</Notice>}
    {research.error && <Notice error>{research.error}</Notice>}
    {ds?.synthetic && <Notice>This is explicitly selected synthetic data. Its returns are not market evidence.</Notice>}
    <div className="row"><button className="primary" disabled={!ds || c.pending || !!research.error} onClick={start}><Play size={16} />{c.pending ? "Starting…" : "Start simulation"}</button></div>
    {(error || c.error) && <Notice error>{error || c.error}</Notice>}
    {!!sessions.length && <div className="sim-past"><span className="eyebrow">Earlier simulations</span>{sessions.map(j => <button key={j.job_id} className={j.portfolio_id === pid ? "active" : ""} onClick={() => onSelect(j.portfolio_id!)}>
      <b>{j.portfolio_id}</b><small>{j.kind === "session" ? "legacy session" : "coordinated"} · {j.status} · {j.progress}{j.total ? ` of ${j.total}` : ""} steps · {stamp(j.created_at)}</small></button>)}</div>}
  </details>;
}
