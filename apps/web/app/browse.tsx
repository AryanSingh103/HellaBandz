"use client";
import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, ArrowUpRight, ShieldCheck, Link2, Scale, TrendingUp, Waves, Repeat2, Radio } from "lucide-react";
import { Band, Event, Manifest, Portfolio, Ranked, View, familyName, money, pct } from "./model";
import { useResource } from "./data";
import { LineChart } from "./charts";
import { Term, ruleText, useHints } from "./help";

type Go = (view: View) => void;

/* ------------------------------------------------------------------ helpers */

export function familyTone(family: string) {
  return family.includes("reversion") ? "rose" : family.includes("vol") ? "blue" : "green";
}
function FamilyIcon({ family, size = 18 }: { family: string; size?: number }) {
  const Icon = family.includes("reversion") ? Repeat2 : family.includes("vol") ? Waves : TrendingUp;
  return <span className={`family-icon ${familyTone(family)}`}><Icon size={size} /></span>;
}
function Ticker({ symbol }: { symbol: string }) {
  return <span className="ticker"><img src={`/assets/${symbol.replace("USDT", "USD")}.png`} alt="" onError={e => { e.currentTarget.style.display = "none"; }} />{symbol}</span>;
}
const signed = (n?: number) => n === undefined || !Number.isFinite(n) ? "—" : `${n >= 0 ? "+" : ""}${(n * 100).toFixed(1)}%`;
const universeOf = (m: Manifest, id: string) => m.fitted.find(f => f.spec.strategy_id === id)?.spec.universe || [];

/* ------------------------------------------------------------------ chart */

type Line = { name: string; color: string; points: [number, number][] }; // [epoch s, % change]

/* ------------------------------------------------------------------ hero */

export function Hero({ manifest, strategies, currency, live, equity, crash, open }: {
  manifest: Manifest; strategies: Ranked[]; currency: string; live: boolean;
  equity: { ts: string; equity: number }[] | undefined; crash?: Band; open: (id: string) => void;
}) {
  const [i, setI] = useState(0);
  useEffect(() => { setI(0); }, [manifest.run_id]);
  const n = strategies.length, s = strategies[Math.min(i, n - 1)];
  const curve = manifest.held_out_test?.equity_curve;
  const lines = useMemo(() => {
    const out: Line[] = [];
    if (curve?.ts?.length) {
      const e0 = curve.equity[0];
      out.push({ name: "Held-out test", color: "#28d49b", points: curve.ts.map((t, k) => [t, curve.equity[k] / e0 - 1]) });
    }
    if (equity && equity.length > 1) {
      const pts = [...equity].reverse();
      const e0 = pts[0].equity;
      out.push({ name: "Live session", color: "#ff4d7d", points: pts.map(p => [Date.parse(p.ts) / 1000, p.equity / e0 - 1]) });
    }
    return out;
  }, [curve, equity]);
  if (!s) return null;
  const alloc = manifest.allocations.by_strategy[s.strategy_id];
  const prop = manifest.proposals.find(p => p.agent_id === s.agent_id);
  const test = manifest.held_out_test.portfolio.metrics;
  const livePts = lines.find(l => l.name === "Live session")?.points;
  const liveRet = livePts?.at(-1)?.[1];
  const day = (t: number) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  return <article className="hero card">
    <div className="hero-left">
      <div className="hero-top">
        <div className="eyebrow-row"><FamilyIcon family={s.family} /><span className="eyebrow">{s.agent_id.replaceAll("_", " ")} agent</span></div>
        {n > 1 && <div className="pager"><button className="round" aria-label="Previous strategy" onClick={() => setI((i - 1 + n) % n)}><ChevronLeft size={18} /></button><span>{i + 1} of {n}</span><button className="round" aria-label="Next strategy" onClick={() => setI((i + 1) % n)}><ChevronRight size={18} /></button></div>}
      </div>
      <h1 className="hero-title">{familyName(s.family)}</h1>
      <div className="hero-status">
        {live ? <><span className="live-dot" /><span className="live-text"><Term k="live">LIVE</Term></span><span className="muted">paper session streaming</span></>
          : <><span className="frozen-dot" /><span className="frozen-text"><Term k="frozen">FROZEN</Term></span><span className="muted">held-out evaluated once</span></>}
      </div>
      <div className="outcomes">
        <div className="outcome"><span className="seed">T</span><span className="o-name"><Term k="training">Training</Term><i className="underline blue" /></span><span className="o-box"><Term k="sr">SR</Term> {s.train.sharpe.toFixed(2)}</span><span className={`pill ${s.train.net_return < 0 ? "neg" : ""}`}>{signed(s.train.net_return)}</span></div>
        <div className="outcome"><span className="seed">V</span><span className="o-name"><Term k="validation">Validation</Term><i className="underline rose" /></span><span className="o-box"><Term k="sr">SR</Term> {s.validation.sharpe.toFixed(2)}</span><span className={`pill ${s.validation.net_return < 0 ? "neg" : ""}`}>{signed(s.validation.net_return)}</span></div>
      </div>
      <div className="hero-meta"><span>{money(alloc?.capital_micros, currency)} <Term k="allocated">allocated</Term> · {pct(alloc?.weight)}</span><button className="text-link" onClick={() => open(s.strategy_id)}>Parameters & gates</button></div>
      <div className="tickers">{universeOf(manifest, s.strategy_id).map(u => <Ticker key={u} symbol={u} />)}</div>
      <p className="news"><b>AI thesis</b> · {prop?.proposal.rationale || "No rationale recorded."}</p>
    </div>
    <div className="hero-right">
      <div className="legend">
        <span><i style={{ background: "#28d49b" }} /><Term k="held-out">Held-out</Term> <b>{signed(test.net_return)}</b></span>
        {liveRet !== undefined && <span><i style={{ background: "#ff4d7d" }} />Live <b>{signed(liveRet)}</b></span>}
        <span className="brand">HellaBandz</span>
      </div>
      <LineChart series={lines.map(l => ({name:l.name,color:l.color,values:l.points}))} format="percent" hero bands={crash ? [crash] : []} />
      <p className="chart-note">Dates are market time of the replayed data.{livePts?.length ? ` Live replay covered ${day(livePts[0][0])} – ${day(livePts[livePts.length - 1][0])}.` : ""}{crash ? " Shaded = scripted crash injected to test the risk engine, not a real event." : ""}</p>
      <div className="chart-foot"><span><Term k="sharpe">Sharpe</Term> <b>{test.sharpe.toFixed(2)}</b></span><span><Term k="max drawdown">Max DD</Term> <b>{pct(test.max_drawdown)}</b></span><span><Term k="fills">Fills</Term> <b>{test.n_fills}</b></span></div>
    </div>
  </article>;
}

/* ------------------------------------------------------------------ sidebar */

export function Sidebar({ manifest, strategies, port, events, currency, go, open, verified }: {
  manifest: Manifest | null; strategies: Ranked[]; port?: Portfolio | null; events: Event[]; currency: string;
  go: Go; open: (id: string) => void; verified?: boolean | null;
}) {
  const st = port?.state;
  const pnl = st ? port!.equity - st.initial_cash : undefined;
  const decisions = events.filter(e => e.type === "OrderAccepted" || e.type === "OrderRejected").slice(0, 6);
  return <aside className="sidebar">
    <button className="promo purple" onClick={() => go("portfolio")}>
      <div><span className="promo-kicker">Live portfolio</span><strong>{port ? money(port.equity, currency) : "Not opened"}</strong>
        <span className={`promo-sub ${pnl !== undefined && pnl < 0 ? "neg" : "pos"}`}>{pnl === undefined ? "Open one from Run" : `${pnl >= 0 ? "▲" : "▼"} ${money(Math.abs(pnl), currency)} since open`}</span></div>
      <span className="promo-art">$</span><ChevronRight size={18} className="promo-chev" />
    </button>
    <button className="promo red" onClick={() => go("audit")}>
      <div><span className="promo-kicker">Ledger audit</span><strong>{verified === false ? "Mismatch" : `${port?.seq ?? 0} events`}</strong>
        <span className="promo-sub">{port ? `digest ${port.digest.slice(0, 10)}…` : "hash-chained · replayable"}</span></div>
      <span className="promo-art"><ShieldCheck size={34} /></span><ChevronRight size={18} className="promo-chev" />
    </button>

    {manifest && <div className="card list-card">
      <button className="list-head" onClick={() => go("research")}><h3>Trending strategies</h3><ChevronRight size={18} /></button>
      {strategies.slice(0, 5).map(s => {
        const w = manifest.allocations.by_strategy[s.strategy_id]?.weight ?? 0;
        return <button key={s.strategy_id} className="list-row-k" onClick={() => open(s.strategy_id)}>
          <span><b>{familyName(s.family)}</b><small>{s.agent_id.replaceAll("_", " ")} · score {s.score.toFixed(2)}</small></span>
          <span className="right"><b>{(w * 100).toFixed(0)}%</b><small className={s.validation.net_return < 0 ? "neg" : "pos"}>{s.validation.net_return < 0 ? "▼" : "▲"} {Math.abs(s.validation.net_return * 100).toFixed(1)}</small></span>
        </button>;
      })}
    </div>}

    <div className="card list-card">
      <button className="list-head" onClick={() => go("orders")}><h3>Risk desk <span className="live-pill"><Radio size={11} />live</span></h3><ChevronRight size={18} /></button>
      {!decisions.length ? <p className="muted small">Decisions stream here as agents trade.</p> : decisions.map(e => {
        const ev = e as Event & { outcome?: string; requested_qty?: number };
        const outcome = e.type === "OrderRejected" ? "REJECT" : ev.outcome || "APPROVE";
        return <div key={e.seq} className="list-row-k static">
          <span><b>{e.side} {ev.requested_qty ?? e.qty} {e.symbol}</b><small title={e.rules?.[0]}>{e.agent_id?.replaceAll("_", " ")}{e.rules?.length ? ` · ${ruleText(e.rules[0])}` : ""}</small></span>
          <span className={`decision ${outcome.toLowerCase()}`}>{outcome}</span>
        </div>;
      })}
    </div>
  </aside>;
}

/* ------------------------------------------------------------------ feature row */

export function Pillars({ go }: { go: Go }) {
  const { on, dismissed } = useHints();
  if (on && !dismissed.includes("how")) return null; // "How it works" covers the same ground
  const items = [
    { icon: <Scale size={20} />, t: "Deterministic risk", d: "Ten rules no AI can override", v: "orders" as View },
    { icon: <ShieldCheck size={20} />, t: "Provable ledger", d: "Replay and verify every cent", v: "audit" as View },
    { icon: <Link2 size={20} />, t: "Sealed on Solana", d: "Tamper-evident research results", v: "research" as View },
  ];
  return <div className="pillars">{items.map(p => <button key={p.t} className="card pillar" onClick={() => go(p.v)}>
    <span className="pillar-icon">{p.icon}</span><span><b>{p.t}</b><small>{p.d}</small></span></button>)}</div>;
}

/* ------------------------------------------------------------------ strategy card */

export function StrategyCard({ manifest, s, open }: { manifest: Manifest; s: Ranked; open: (id: string) => void }) {
  const allocation = manifest.allocations.by_strategy[s.strategy_id];
  const source = manifest.proposals.find(p => p.agent_id === s.agent_id)?.source || "unavailable";
  const v = s.validation.net_return, t = s.train.net_return;
  const total = Math.abs(v) + Math.abs(t) || 1;
  return <button className="card strategy-card" onClick={() => open(s.strategy_id)} aria-label={`View ${familyName(s.family)}`}>
    <div className="sc-top"><FamilyIcon family={s.family} /><span className="eyebrow">{s.agent_id.replaceAll("_", " ")}</span><span className={`src ${source}`}>{source === "llm" ? "AI" : source}</span></div>
    <h2>{familyName(s.family)}</h2>
    <div className="tickers">{universeOf(manifest, s.strategy_id).map(u => <Ticker key={u} symbol={u} />)}</div>
    <div className="sc-row"><span><Term k="training">Training</Term></span><span className="muted">SR {s.train.sharpe.toFixed(2)}</span><span className={`pill sm ${t < 0 ? "neg" : ""}`}>{signed(t)}</span></div>
    <div className="sc-row"><span><Term k="validation">Validation</Term></span><span className="muted">SR {s.validation.sharpe.toFixed(2)}</span><span className={`pill sm ${v < 0 ? "neg" : ""}`}>{signed(v)}</span></div>
    {s.excess && <div className="sc-row"><span><Term k="vs core">vs core</Term></span><span className="muted">IR {s.excess.out_of_sample_information_ratio.toFixed(2)}</span><span className={`pill sm ${s.excess.out_of_sample_excess_return < 0 ? "neg" : ""}`}>{signed(s.excess.out_of_sample_excess_return)}</span></div>}
    <div className="meter"><i style={{ width: `${Math.abs(t) / total * 100}%` }} className="blue" /><i style={{ width: `${Math.abs(v) / total * 100}%` }} className="rose" /></div>
    <footer><span>{pct(allocation?.weight)} <Term k="allocated">allocated</Term></span><span className={s.gates.length ? "neg" : "pos"}><Term k="gates">{s.gates.length ? `${s.gates.length} gate${s.gates.length > 1 ? "s" : ""} failed` : "Gates passed"}</Term><ArrowUpRight size={13} /></span></footer>
  </button>;
}

export function useLiveEquity(pid: string | null, revision: number) {
  return useResource<{ ts: string; equity: number }[]>(pid ? "/v1/equity" : null, { portfolio_id: pid || undefined, limit: 2000 }, 10000, revision);
}
