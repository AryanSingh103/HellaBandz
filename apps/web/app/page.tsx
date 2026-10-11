"use client";
import { useEffect, useState } from "react";
import { Search, SlidersHorizontal, ArrowUpRight, RefreshCw, Activity, ChevronDown } from "lucide-react";
import { Context, Dataset, initialRoute, Portfolio, PortfolioChoice, Route, Runtime, Tournament, View, Job, crashBand, comparePerformance, matchesSearch, familyName, pct, money, validId } from "./model";
import { useEventStream, useResource } from "./data";
import { Empty, Metric, Modal, Notice, JsonView } from "./ui";
import { CoordinatorView } from "./coordinator";
import { PortfolioView, AuditView, AnalyticsView } from "./portfolio";
import { OrdersView } from "./orders";
import { ResearchView, RunView } from "./research";
import { Ask } from "./voice";
import { MarketplaceView } from "./marketplace";
import { Hero, Pillars, Sidebar, StrategyCard, useLiveEquity } from "./browse";
import { GuideToggle, HintsProvider, HowItWorks, PageIntro } from "./help";
import { AccessGate, SignOut } from "./access";

const views: View[] = ["browse", "marketplace", "portfolio", "orders", "research", "audit", "analytics", "coordinator", "run"];
const categories = ["All strategies", "Momentum", "Reversion", "Volatility", "Equities", "Crypto"];
function readRoute(): Route {
  const q = new URLSearchParams(location.search), route = { ...initialRoute };
  for (const k of Object.keys(route) as (keyof Route)[]) if (q.has(k)) Object.assign(route, { [k]: q.get(k) || "" });
  if (!validId(route.p)) route.p = "main";
  if (route.run && !validId(route.run)) route.run = "";
  if (!views.includes(route.view)) route.view = "browse";
  return route;
}
export default function Page(){return <AccessGate><Terminal/></AccessGate>;}
function Terminal() {
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
  const observing = ["browse", "portfolio", "orders", "audit"].includes(route.view);
  const researching = ["browse", "research", "run"].includes(route.view);
  const charting = ["browse", "portfolio"].includes(route.view);
  const health = useResource<{api:string}>(observing ? null : "/healthz", {}, 15000, revision);
  const portfolios = useResource<PortfolioChoice[]>("/v1/portfolios", {}, 30000, revision);
  const datasets = useResource<Dataset[]>(route.view === "marketplace" ? null : "/v1/datasets", {}, 0, revision);
  const runs = useResource<Tournament[]>(researching ? "/v1/tournaments" : null, { limit: 100 }, 60000, revision);
  const context = useResource<Context>(route.view === "marketplace" ? null : "/v1/context", { portfolio_id: route.p }, 0, revision);
  const port = useResource<Portfolio>(observing ? "/v1/portfolio" : null, { portfolio_id: route.p }, 10000, revision, true);
  const runtime = useResource<Runtime>(observing ? "/v1/runtime" : null, { portfolio_id: route.p }, 5000, revision);
  const stream = useEventStream(observing ? route.p : null);
  const equity = useLiveEquity(charting ? route.p : null, revision);
  const jobs = useResource<Job[]>(charting ? "/v1/jobs" : null, { portfolio_id: route.p, include_cleared: "true" }, 30000, revision);
  const crash = crashBand(jobs.data || undefined, route.p, equity.data || undefined);
  const manifest = (route.run ? runs.data?.find(t => t.run_id === route.run)?.manifest : context.data?.manifest) || null;
  const dataset = datasets.data?.find(d => d.content_sha256 === manifest?.dataset.content_sha256 || (!manifest && d.dataset_id === context.data?.dataset_id));
  const portfolioDataset = datasets.data?.find(d => d.dataset_id === context.data?.dataset_id || d.content_sha256 === context.data?.manifest?.dataset.content_sha256);
  const currency = dataset?.quote_currency || "USD", connected = observing ? !runtime.error && !!runtime.data : !health.error && !!health.data;
  const scope = `${route.p}:${manifest?.run_id || "unassociated"}`;
  const visible = (manifest?.ranked || []).filter(s => {
    const universe = manifest?.fitted.find(f => f.spec.strategy_id === s.strategy_id)?.spec.universe || [];
    const c = route.category, crypto = universe.some(u => /BTC|ETH|USDT|USD$/.test(u));
    return (!route.dataset || manifest?.dataset.dataset_id === route.dataset) &&
      (c === "All strategies" || c === "Momentum" && /momentum|crossover|breakout/.test(s.family + s.agent_id) || c === "Reversion" && /reversion/.test(s.family) || c === "Volatility" && /vol/.test(s.family) || c === "Equities" && !crypto || c === "Crypto" && crypto) &&
      matchesSearch(route.q, familyName(s.family), s.family, s.strategy_id, s.agent_id, ...universe);
  }).sort((a,b) => route.sort === "return" ? comparePerformance(a.validation, b.validation, "net_return") : route.sort === "risk" ? comparePerformance(a.validation, b.validation, "max_drawdown") : route.sort === "name" ? familyName(a.family).localeCompare(familyName(b.family)) : b.score - a.score);
  const detail = manifest?.ranked.find(s => s.strategy_id === strategy);
  const live = connected && !!runtime.data?.worker_alive && !!port.data && Date.now() - Date.parse(port.data.updated_at) < 20000 && stream.events.some(e => e.type === "BarApplied");
  const decisionsCount = stream.events.filter(e => e.type === "OrderAccepted" || e.type === "OrderRejected").length;
  return <HintsProvider>
    <header className="site-header"><div className="header-inner">
      <a className="wordmark" href="/" onClick={e => { e.preventDefault(); navigate({ view: "browse" }); }}aria-label="HellaBandz home"><span className="wm-hella">Hella</span><span className="wm-bandz">Band<span className="wm-z">z</span></span></a>
      <nav aria-label="Main navigation">{views.map(v => <a key={v} href={`/?view=${v}&p=${encodeURIComponent(route.p)}`} aria-current={route.view === v ? "page" : undefined} onClick={e => { e.preventDefault(); navigate({ view: v, order: "" }); }}>{v === "browse" ? "Markets" : v}{v === "orders" && decisionsCount > 0 && <span className="nav-count">{decisionsCount}</span>}</a>)}
        <span className={`nav-live ${live ? "on" : ""}`}><i />Live</span></nav>
      <div className="header-search"><Search size={17} /><input aria-label="Search strategies" placeholder="Trade on any strategy" value={route.q} onChange={e => navigate({ q: e.target.value, view: "browse" })} /></div>
      <GuideToggle />
      <SignOut />
      <button className="ghost-pill" onClick={() => setAskOpen(!askOpen)}><Activity size={15} />Ask</button>
      <button className="cta-pill" onClick={() => navigate({ view: "run" })}>Run research</button>
    </div><div className="category-inner"><nav aria-label="Strategy categories">{categories.map(c => <a key={c} href={`/?category=${encodeURIComponent(c)}`} className={route.category === c && route.view === "browse" ? "selected" : ""} onClick={e => { e.preventDefault(); navigate({ category: c, view: "browse" }); }}>{c === "All strategies" ? "Trending" : c}</a>)}</nav><span className="simulation"><span className={`status-dot ${connected ? "online" : ""}`} />Simulated · paper trading</span></div></header>
    <main>
      <div className="scopebar"><div className="row"><span className={`status-dot ${connected ? "online" : ""}`} /><span>{connected ? "API connected" : (observing ? runtime.loading : health.loading) ? "Connecting" : "API unavailable"}</span><span className="scope-divider">/</span><label className="inline">Portfolio<select aria-label="Portfolio" value={route.p} onChange={e => { setPortfolioInput(e.target.value); navigate({ p: e.target.value, run: "", order: "" }); }}>{!portfolios.data?.some(p => p.portfolio_id === route.p) && <option>{route.p}</option>}{portfolios.data?.map(p => <option key={p.portfolio_id}>{p.portfolio_id}</option>)}</select></label></div><div className="row"><span>Stream {stream.status}</span><span className="scope-divider">/</span><span>Worker {!observing ? "not observed" : connected ? runtime.data?.intentional_down ? "intentionally down" : runtime.data?.worker_alive ? "healthy" : "down" : "unknown"}</span><span className="scope-divider">/</span><span className="muted">{observing && connected ? `${runtime.data?.pending_commands} queued` : "Queue not observed"}</span><button className="icon" title="Refresh data" aria-label="Refresh data" onClick={() => refresh(n => n + 1)}><RefreshCw size={14} /></button></div></div>
      {(runtime.error || port.error || context.error || runs.error) && <Notice error>Data unavailable: {runtime.error || port.error || context.error || runs.error}. {port.time ? `Last portfolio update ${new Date(port.time).toLocaleTimeString()} (stale).` : "Retry when the API is available."}</Notice>}
      {route.view === "browse" && <div className="home-grid"><div className="home-main">
        <PageIntro view="browse" manifest={manifest} />
        {!manifest ? <div className="card"><Empty>{context.loading || runs.loading ? "Loading research context..." : "No research associated with this portfolio."}<div className="row centered"><button className="cta-pill" onClick={() => navigate({ view: "run" })}>Run research <ArrowUpRight size={15} /></button>{!!runs.data?.length && <button onClick={() => navigate({ run: runs.data![0].run_id })}>Browse latest research</button>}</div></Empty></div>
          : visible.length ? <Hero manifest={manifest} strategies={visible} currency={currency} live={live} equity={equity.data || undefined} crash={crash} open={setStrategy} /> : null}
        <HowItWorks go={v => navigate({ view: v })} />
        <Pillars go={v => navigate({ view: v })} />
        <div className="browse-title"><h2 className="section-h">{route.category === "All strategies" ? "All strategies" : route.category}</h2><div className="filters">
          <label className="select-wrap"><span className="sr-only">Research run</span><select aria-label="Research run" value={route.run} onChange={e => navigate({ run: e.target.value, dataset: "" })}><option value="">{context.data?.run_id ? `Associated: ${context.data.run_id}` : "Portfolio: unassociated"}</option>{runs.data?.map(t => <option key={t.run_id} value={t.run_id}>{t.run_id}</option>)}</select><ChevronDown size={14} /></label>
          <label className="select-wrap"><span className="sr-only">Sort strategies</span><select aria-label="Sort strategies" value={route.sort} onChange={e => navigate({ sort: e.target.value })}><option value="score">Top score</option><option value="return">Validation return</option><option value="risk">Lowest drawdown</option><option value="name">Name</option></select><ChevronDown size={14} /></label>
          <label className="select-wrap"><span className="sr-only">Dataset filter</span><select aria-label="Dataset filter" value={route.dataset} onChange={e => navigate({ dataset: e.target.value })}><option value="">All datasets</option>{datasets.data?.map(d => <option key={d.dataset_id}>{d.dataset_id}</option>)}</select><ChevronDown size={14} /></label>
          <button className="icon round" title="Reset browse filters" aria-label="Reset browse filters" onClick={() => navigate({ q: "", category: "All strategies", dataset: "", sort: "score" })}><SlidersHorizontal size={16} /></button>
        </div></div>
        {manifest && <div className="browse-context"><span>{manifest.dataset.dataset_id} <span className="muted">· {manifest.dataset.source}</span></span><span>{visible.length} strategies</span></div>}
        {manifest && (!visible.length ? <div className="card"><Empty>No strategies match these filters.<button onClick={() => navigate({ q: "", category: "All strategies", dataset: "" })}>Clear filters</button></Empty></div> : <div className="strategy-grid">{visible.map(s => <StrategyCard key={s.strategy_id} manifest={manifest} s={s} open={setStrategy} />)}</div>)}
        {manifest && <div className="research-summary card"><div><span className="eyebrow">Frozen held-out evaluation</span><h2>Portfolio performance</h2></div><Metric label="Net return" value={<span className={manifest.held_out_test.portfolio.metrics.net_return < 0 ? "neg" : "pos"}>{pct(manifest.held_out_test.portfolio.metrics.net_return)}</span>} /><Metric label="Sharpe" value={manifest.held_out_test.portfolio.metrics.sharpe.toFixed(2)} /><Metric label="Max drawdown" value={pct(manifest.held_out_test.portfolio.metrics.max_drawdown)} />{manifest.core && <Metric label="Held in passive core" value={pct(manifest.allocations.by_strategy[manifest.core.spec.strategy_id]?.weight)} />}<Metric label="Initial virtual capital" value={money(manifest.policy.initial_cash_micros, currency)} /><button className="cta-pill" onClick={() => navigate({ view: "research" })}>Research record <ArrowUpRight size={16} /></button></div>}
      </div>
      <Sidebar manifest={manifest} strategies={visible} port={port.data} events={stream.events} currency={currency} go={v => navigate({ view: v })} open={setStrategy} />
      </div>}
      {route.view !== "browse" && <div className="view"><PageIntro view={route.view} manifest={manifest} />
      {route.view === "marketplace" && <MarketplaceView />}
      {route.view === "coordinator" && <CoordinatorView key={scope} pid={route.p} />}
      {route.view === "portfolio" && <PortfolioView key={scope} crash={crash} pid={route.p} port={port.data} runtime={connected ? runtime.data : undefined} dataset={portfolioDataset} refresh={() => refresh(n=>n+1)} onSelect={p => { setPortfolioInput(p); navigate({ p, run: "", order: "" }); }} />}
      {route.view === "orders" && <OrdersView key={scope} pid={route.p} port={port.data} dataset={portfolioDataset} orderId={route.order} selectOrder={order => navigate({ order })} refresh={() => refresh(n=>n+1)} />}
      {route.view === "research" && <ResearchView key={scope} pid={route.p} manifest={manifest} explicit={!!route.run} runs={runs.data || []} selected={route.run} select={run => navigate({ run })} />}
      {route.view === "audit" && <AuditView key={scope} pid={route.p} port={port.data} events={stream.events} streamStatus={stream.status} />}
      {route.view === "analytics" && <AnalyticsView key={scope} pid={route.p} currency={portfolioDataset?.quote_currency || "USD"} />}
      {route.view === "run" && <RunView key={route.p}datasets={datasets.data || []} runs={runs.data || []} selectedRun={manifest?.run_id || ""} onSelect={p => { setPortfolioInput(p); navigate({ p, run: "", view: "portfolio" }); }} refresh={() => refresh(n=>n+1)} />}</div>}
      <footer className="site-footer"><span>HellaBandz <span className="muted">/ Historical simulation. Long-only. Cash-funded.</span></span><form className="row" onSubmit={e => { e.preventDefault(); if (!validId(portfolioInput)) { setIdError("Use 1-96 letters, digits, underscore, dot, colon or hyphen."); return; } setIdError(""); navigate({ p: portfolioInput, run: "", order: "" }); }}><input aria-label="Portfolio ID" value={portfolioInput} onChange={e => setPortfolioInput(e.target.value)} placeholder="Portfolio ID" /><button type="submit" className="icon" title="Select portfolio" aria-label="Select portfolio"><ArrowUpRight size={17} /></button></form>{idError && <Notice error>{idError}</Notice>}</footer>
    </main>
    {detail && manifest && <Modal title={familyName(detail.family)} close={() => setStrategy("")}><p className="muted">{detail.strategy_id}</p><div className="metrics"><Metric label="Allocated capital" value={money(manifest.allocations.by_strategy[detail.strategy_id]?.capital_micros, currency)} /><Metric label="Validation return" value={pct(detail.validation.net_return)} /></div><p>{manifest.proposals.find(p=>p.agent_id===detail.agent_id)?.proposal.rationale}</p><JsonView label="Full proposal and evidence" value={manifest.proposals.find(p=>p.agent_id===detail.agent_id)} /><JsonView label="Parameters, gates and metrics" value={detail} /><button className="cta-pill" onClick={() => navigate({ view: "research" })}>Open research <ArrowUpRight size={16} /></button></Modal>}
    {askOpen && <Modal title={`Ask HellaBandz / ${route.p}`} close={() => setAskOpen(false)}><PageIntro view="ask" manifest={manifest} /><Ask key={route.p}pid={route.p} /></Modal>}
  </HintsProvider>;
}
