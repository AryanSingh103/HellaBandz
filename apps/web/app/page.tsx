"use client";
import { useEffect, useState } from "react";
import { Search, SlidersHorizontal, ArrowUpRight, RefreshCw, Activity, ChevronDown } from "lucide-react";
import { Context, Dataset, initialRoute, Portfolio, PortfolioChoice, Route, Runtime, Tournament, View, familyName, pct, money, validId } from "./model";
import { useEventStream, useResource } from "./data";
import { Empty, Metric, Modal, Notice, JsonView } from "./ui";
import { PortfolioView, AuditView, AnalyticsView } from "./portfolio";
import { OrdersView } from "./orders";
import { ResearchView, RunView } from "./research";
import { Ask } from "./voice";
import { MarketplaceView } from "./marketplace";

const views: View[] = ["browse", "marketplace", "portfolio", "orders", "research", "audit", "analytics", "run"];
const categories = ["All strategies", "Momentum", "Reversion", "Volatility", "Equities", "Crypto"];
function readRoute(): Route {
  const q = new URLSearchParams(location.search), route = { ...initialRoute };
  for (const k of Object.keys(route) as (keyof Route)[]) if (q.has(k)) Object.assign(route, { [k]: q.get(k) || "" });
  if (!validId(route.p)) route.p = "main";
  if (route.run && !validId(route.run)) route.run = "";
  if (!views.includes(route.view)) route.view = "browse";
  return route;
}
export default function Page() {
  const [route, setRoute] = useState(initialRoute), [revision, refresh] = useState(0);
  const [strategy, setStrategy] = useState(""), [askOpen, setAskOpen] = useState(false);
  const [portfolioInput, setPortfolioInput] = useState("main"), [idError, setIdError] = useState("");
  useEffect(() => {
    const sync = () => { const r = readRoute(); setRoute(r); setPortfolioInput(r.p); setStrategy(""); };
    sync(); window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  function navigate(change: Partial<Route>) {
    const r = { ...route, ...change }, q = new URLSearchParams();
    Object.entries(r).forEach(([k,v]) => { if (v && v !== initialRoute[k as keyof Route]) q.set(k,v); });
    history.pushState(null, "", `/${q.size ? "?" + q : ""}`); setRoute(r); setStrategy("");
  }
  const portfolios = useResource<PortfolioChoice[]>("/v1/portfolios", {}, 6000, revision);
  const datasets = useResource<Dataset[]>("/v1/datasets", {}, 30000, revision);
  const runs = useResource<Tournament[]>("/v1/tournaments", { limit: 100 }, 6000, revision);
  const context = useResource<Context>("/v1/context", { portfolio_id: route.p }, 6000, revision);
  const port = useResource<Portfolio>("/v1/portfolio", { portfolio_id: route.p }, 2000, revision, true);
  const runtime = useResource<Runtime>("/v1/runtime", { portfolio_id: route.p }, 2000, revision);
  const stream = useEventStream(route.p);
  const manifest = (route.run ? runs.data?.find(t => t.run_id === route.run)?.manifest : context.data?.manifest) || null;
  const dataset = datasets.data?.find(d => d.content_sha256 === manifest?.dataset.content_sha256 || (!manifest && d.dataset_id === context.data?.dataset_id));
  const portfolioDataset = datasets.data?.find(d => d.dataset_id === context.data?.dataset_id || d.content_sha256 === context.data?.manifest?.dataset.content_sha256);
  const currency = dataset?.quote_currency || "USD", connected = !runtime.error && !!runtime.data;
  const scope = `${route.p}:${manifest?.run_id || "unassociated"}`;
  const visible = (manifest?.ranked || []).filter(s => {
    const universe = manifest?.fitted.find(f => f.spec.strategy_id === s.strategy_id)?.spec.universe || [];
    const c = route.category, crypto = universe.some(u => /BTC|ETH|USDT|USD$/.test(u));
    return (!route.dataset || manifest?.dataset.dataset_id === route.dataset) &&
      (c === "All strategies" || c === "Momentum" && /momentum|crossover|breakout/.test(s.family + s.agent_id) || c === "Reversion" && /reversion/.test(s.family) || c === "Volatility" && /vol/.test(s.family) || c === "Equities" && !crypto || c === "Crypto" && crypto) &&
      `${familyName(s.family)} ${s.family} ${s.strategy_id} ${s.agent_id} ${universe.join(" ")}`.toLowerCase().includes(route.q.toLowerCase());
  }).sort((a,b) => route.sort === "return" ? b.validation.net_return - a.validation.net_return : route.sort === "risk" ? a.validation.max_drawdown - b.validation.max_drawdown : route.sort === "name" ? familyName(a.family).localeCompare(familyName(b.family)) : b.score - a.score);
  const detail = manifest?.ranked.find(s => s.strategy_id === strategy);
  return <>
    <header className="site-header"><div className="header-inner">
      <a className="wordmark" href="/" onClick={e => { e.preventDefault(); navigate({ view: "browse" }); }}>QuantSwarm</a>
      <nav aria-label="Main navigation">{views.map(v => <a key={v} href={`/?view=${v}&p=${encodeURIComponent(route.p)}`} aria-current={route.view === v ? "page" : undefined} onClick={e => { e.preventDefault(); navigate({ view: v, order: "" }); }}>{v[0].toUpperCase()+v.slice(1)}</a>)}</nav>
      <div className="header-search"><Search size={17} /><input aria-label="Search strategies" placeholder="Search strategies, agents, symbols" value={route.q} onChange={e => navigate({ q: e.target.value, view: "browse" })} /></div>
      <button className="ask-button" onClick={() => setAskOpen(!askOpen)}><Activity size={16} />Ask</button>
    </div><div className="category-inner"><nav aria-label="Strategy categories">{categories.map(c => <a key={c} href={`/?category=${encodeURIComponent(c)}`} className={route.category === c ? "selected" : ""} onClick={e => { e.preventDefault(); navigate({ category: c, view: "browse" }); }}>{c}</a>)}</nav><span className="simulation">Simulated trading</span></div></header>
    <main>
      <div className="scopebar"><div className="row"><span className={`status-dot ${connected ? "online" : ""}`} /><span>{connected ? "API connected" : runtime.loading ? "Connecting" : "API unavailable"}</span><span className="scope-divider">/</span><label className="inline">Portfolio<select aria-label="Portfolio" value={route.p} onChange={e => { setPortfolioInput(e.target.value); navigate({ p: e.target.value, run: "", order: "" }); }}>{!portfolios.data?.some(p => p.portfolio_id === route.p) && <option>{route.p}</option>}{portfolios.data?.map(p => <option key={p.portfolio_id}>{p.portfolio_id}</option>)}</select></label></div><div className="row"><span>Stream {stream.status}</span><span>Worker {connected ? runtime.data?.intentional_down ? "intentionally down" : runtime.data?.worker_alive ? "healthy" : "down" : "unknown"}</span><span className="muted">{connected ? `${runtime.data?.pending_commands} queued` : "Queue unknown"}</span><button className="icon" title="Refresh data" aria-label="Refresh data" onClick={() => refresh(n => n + 1)}><RefreshCw size={15} /></button></div></div>
      {(runtime.error || port.error || context.error || runs.error) && <Notice error>Data unavailable: {runtime.error || port.error || context.error || runs.error}. {port.time ? `Last portfolio update ${new Date(port.time).toLocaleTimeString()} (stale).` : "Retry when the API is available."}</Notice>}
      {route.view === "browse" && <>
        <div className="browse-title"><h1>Browse Strategies</h1><div className="filters">
          <label className="select-wrap"><span className="sr-only">Research run</span><select aria-label="Research run" value={route.run} onChange={e => navigate({ run: e.target.value, dataset: "" })}><option value="">{context.data?.run_id ? `Associated: ${context.data.run_id}` : "Portfolio: unassociated"}</option>{runs.data?.map(t => <option key={t.run_id} value={t.run_id}>{t.run_id}</option>)}</select><ChevronDown size={14} /></label>
          <label className="select-wrap"><span className="sr-only">Sort strategies</span><select aria-label="Sort strategies" value={route.sort} onChange={e => navigate({ sort: e.target.value })}><option value="score">Top score</option><option value="return">Validation return</option><option value="risk">Lowest drawdown</option><option value="name">Name</option></select><ChevronDown size={14} /></label>
          <label className="select-wrap"><span className="sr-only">Dataset filter</span><select aria-label="Dataset filter" value={route.dataset} onChange={e => navigate({ dataset: e.target.value })}><option value="">All datasets</option>{datasets.data?.map(d => <option key={d.dataset_id}>{d.dataset_id}</option>)}</select><ChevronDown size={14} /></label>
          <button className="icon" title="Reset browse filters" aria-label="Reset browse filters" onClick={() => navigate({ q: "", category: "All strategies", dataset: "", sort: "score" })}><SlidersHorizontal size={17} /></button>
        </div></div>
        {manifest && <div className="browse-context"><span>{manifest.dataset.dataset_id} <span className="muted">/ {manifest.dataset.source}</span></span><span>{visible.length} strategies</span></div>}
        {!manifest ? <Empty>{context.loading || runs.loading ? "Loading research context..." : "No research associated with this portfolio."}<div className="row centered"><button onClick={() => navigate({ view: "run" })}>Run research <ArrowUpRight size={15} /></button>{!!runs.data?.length && <button onClick={() => navigate({ run: runs.data![0].run_id })}>Browse latest research</button>}</div></Empty> : !visible.length ? <Empty>No strategies match these filters.<button onClick={() => navigate({ q: "", category: "All strategies", dataset: "" })}>Clear filters</button></Empty> : <div className="strategy-grid">{visible.map(s => {
          const universe = manifest.fitted.find(f => f.spec.strategy_id === s.strategy_id)?.spec.universe || [];
          const source = manifest.proposals.find(p => p.agent_id === s.agent_id)?.source || "unavailable";
          const allocation = manifest.allocations.by_strategy[s.strategy_id];
          return <button className="strategy-card" key={s.strategy_id} onClick={() => setStrategy(s.strategy_id)} aria-label={`View ${familyName(s.family)}`}>
            <div className="card-top"><div className={`family-icon ${s.family.includes("reversion") ? "rose" : s.family.includes("vol") ? "blue" : "green"}`}><Activity size={19} /></div><span>{s.agent_id.replaceAll("_", " ")}</span><span className="card-source">{source}</span></div>
            <h2>{familyName(s.family)}</h2><div className="tickers">{universe.map(u => <span key={u}><img src={`/assets/${u.replace("USDT", "USD")}.png`} alt="" onError={e => { e.currentTarget.style.display = "none"; }} />{u}</span>)}</div>
            <div className="performance"><span><i className="line-mark blue" />Training return</span><span className="muted">SR {s.train.sharpe.toFixed(2)}</span><strong className={s.train.net_return < 0 ? "negative" : ""}>{pct(s.train.net_return)}</strong></div>
            <div className="performance"><span><i className="line-mark rose" />Validation return</span><span className="muted">SR {s.validation.sharpe.toFixed(2)}</span><strong className={s.validation.net_return < 0 ? "negative" : ""}>{pct(s.validation.net_return)}</strong></div>
            <div className="allocation"><span>Allocation <b>{pct(allocation?.weight)}</b></span><span>Max drawdown <b>{pct(s.validation.max_drawdown)}</b></span></div>
            <footer><span>{s.validation.n_fills} validation fills</span><span>{s.gates.length ? `${s.gates.length} gates failed` : "Gates passed"}<ArrowUpRight size={13} /></span></footer>
          </button>;
        })}</div>}
        {manifest && <div className="research-summary"><div><span className="eyebrow">Frozen held-out evaluation</span><h2>Portfolio performance</h2></div><Metric label="Net return" value={pct(manifest.held_out_test.portfolio.metrics.net_return)} /><Metric label="Max drawdown" value={pct(manifest.held_out_test.portfolio.metrics.max_drawdown)} /><Metric label="Initial virtual capital" value={money(manifest.policy.initial_cash_micros, currency)} /><button onClick={() => navigate({ view: "research" })}>Research record <ArrowUpRight size={16} /></button></div>}
      </>}
      {route.view === "marketplace" && <MarketplaceView />}
      {route.view === "portfolio" && <PortfolioView key={scope} pid={route.p} port={port.data} runtime={connected ? runtime.data : undefined} dataset={portfolioDataset} refresh={() => refresh(n=>n+1)} onSelect={p => { setPortfolioInput(p); navigate({ p, run: "", order: "" }); }} />}
      {route.view === "orders" && <OrdersView key={scope} pid={route.p} port={port.data} dataset={portfolioDataset} orderId={route.order} selectOrder={order => navigate({ order })} refresh={() => refresh(n=>n+1)} />}
      {route.view === "research" && <ResearchView key={scope} pid={route.p} manifest={manifest} explicit={!!route.run} runs={runs.data || []} selected={route.run} select={run => navigate({ run })} />}
      {route.view === "audit" && <AuditView key={scope} pid={route.p} port={port.data} events={stream.events} streamStatus={stream.status} />}
      {route.view === "analytics" && <AnalyticsView key={scope} pid={route.p} currency={portfolioDataset?.quote_currency || "USD"} />}
      {route.view === "run" && <RunView key={route.p}datasets={datasets.data || []} runs={runs.data || []} selectedRun={manifest?.run_id || ""} onSelect={p => { setPortfolioInput(p); navigate({ p, run: "", view: "portfolio" }); }} refresh={() => refresh(n=>n+1)} />}
      <footer className="site-footer"><span>QuantSwarm <span className="muted">/ Historical simulation. Long-only. Cash-funded.</span></span><form className="row" onSubmit={e => { e.preventDefault(); if (!validId(portfolioInput)) { setIdError("Use 1-96 letters, digits, underscore, dot, colon or hyphen."); return; } setIdError(""); navigate({ p: portfolioInput, run: "", order: "" }); }}><input aria-label="Portfolio ID" value={portfolioInput} onChange={e => setPortfolioInput(e.target.value)} placeholder="Portfolio ID" /><button type="submit" className="icon" title="Select portfolio" aria-label="Select portfolio"><ArrowUpRight size={17} /></button></form>{idError && <Notice error>{idError}</Notice>}</footer>
    </main>
    {detail && manifest && <Modal title={familyName(detail.family)} close={() => setStrategy("")}><p className="muted">{detail.strategy_id}</p><div className="metrics"><Metric label="Allocated capital" value={money(manifest.allocations.by_strategy[detail.strategy_id]?.capital_micros, currency)} /><Metric label="Validation return" value={pct(detail.validation.net_return)} /></div><p>{manifest.proposals.find(p=>p.agent_id===detail.agent_id)?.proposal.rationale}</p><JsonView label="Full proposal and evidence" value={manifest.proposals.find(p=>p.agent_id===detail.agent_id)} /><JsonView label="Parameters, gates and metrics" value={detail} /><button className="primary" onClick={() => navigate({ view: "research" })}>Open research <ArrowUpRight size={16} /></button></Modal>}
    {askOpen && <Modal title={`Ask QuantSwarm / ${route.p}`} close={() => setAskOpen(false)}><Ask key={route.p}pid={route.p} /></Modal>}
  </>;
}
