"use client";
import { FormEvent, useRef, useState } from "react";
import { ArrowUpRight, BarChart3, FlaskConical, ShoppingBag, Search, Wallet, RefreshCw } from "lucide-react";
import { errorText, id, Metrics, pct, post, request, stamp } from "./model";
import { useResource } from "./data";
import { Empty, JsonView, Metric, Modal, Notice } from "./ui";

type Split = "train" | "validation" | "test";
type Asset = { asset_id: string; title: string; family: string; agent_id: string; status: string; universe: string[];
  description: string; metrics: Record<Split, Metrics | null>; dataset_id: string | null; source: string; run_id: string | null;
  periods: Record<string, number[]> | null; gates: string[]; params: Record<string, number>; note: string; dataset_hash?: string };
type Offer = { listing_id: string; asset_id: string; seller_id: string; price: number; remaining: number };
type Trade = { request_id: string; asset_id: string; buyer_id: string; seller_id: string; price: number; quantity: number; total: number; created_at: string };
type Catalog = { assets: { descriptor: Asset }[]; listings: Offer[]; accounts: { account_id: string }[] };
type Balance = { account_id: string; balance: number; holdings: { asset_id: string; quantity: number; descriptor: Asset }[]; listings: Offer[]; trades: Trade[] };
const credits = (micros: number) => `${(micros / 1e6).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} cr`;
const num = (n?: number | null) => n == null || !Number.isFinite(n) ? "Unavailable" : n.toFixed(2);
const label: Record<Split, string> = { train: "Training", validation: "Validation", test: "Held-out test" };
const period = (a: Asset, split: Split) => {
  const range = a.periods?.[split];
  return range?.length === 2 ? range.map(n => new Date(n * 1000).toISOString().slice(0, 10)).join(" → ") : "Unavailable";
};

// The same form retry retains its request ID after a timeout, preventing a second purchase.
function useMarketAction(done: () => void) {
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const retry = useRef<{ signature: string; request_id: string } | null>(null), busy = useRef(false);
  async function send(path: string, body: Record<string, unknown>, keyed = true) {
    if (busy.current) return false;
    const signature = JSON.stringify([path, body]);
    if (retry.current?.signature !== signature) retry.current = { signature, request_id: id("market") };
    busy.current = true; setPending(true); setError("");
    try {
      await request(path, {}, post(keyed ? { ...body, request_id: retry.current.request_id } : body));
      retry.current = null; done(); return true;
    } catch (e) { setError(errorText(e)); return false; }
    finally { busy.current = false; setPending(false); }
  }
  return { send, pending, error };
}

export function MarketplaceView() {
  const [account, setAccount] = useState("demo-buyer"), [revision, refresh] = useState(0);
  const [split, setSplit] = useState<Split>("validation"), [query, setQuery] = useState(""), [sort, setSort] = useState("sharpe");
  const [tab, setTab] = useState("discover"), [selected, select] = useState<string[]>([]);
  const [detail, setDetail] = useState<Asset | null>(null), [ticket, setTicket] = useState<Asset | null>(null);
  const [resell, setResell] = useState<Asset | null>(null), [reviewOpen, setReviewOpen] = useState(false), [message, setMessage] = useState("");
  const catalog = useResource<Catalog>("/v1/marketplace", {}, 10000, revision);
  const wallet = useResource<Balance>("/v1/marketplace/wallet", { account_id: account }, 5000, revision);
  const assets = catalog.data?.assets.map(a => a.descriptor) || [], offers = catalog.data?.listings || [];
  const changed = () => refresh(n => n + 1);
  const action = useMarketAction(changed);
  const available = (a: Asset) => offers.filter(o => o.asset_id === a.asset_id && o.seller_id !== account).sort((a, b) => a.price - b.price);
  const filtered = assets.filter(a => `${a.title} ${a.agent_id} ${a.universe.join(" ")}`.toLowerCase().includes(query.toLowerCase())).sort((a,b) => {
    if (sort === "price") return (available(a)[0]?.price ?? Infinity) - (available(b)[0]?.price ?? Infinity);
    const field = sort === "return" ? "net_return" : sort === "drawdown" ? "max_drawdown" : "sharpe";
    const av = a.metrics[split]?.[field], bv = b.metrics[split]?.[field];
    return av == null ? bv == null ? a.title.localeCompare(b.title) : 1 : bv == null ? -1 : sort === "drawdown" ? av - bv : bv - av;
  });
  const comparison = assets.filter(a => selected.includes(a.asset_id));
  return <>
    <div className="market-hero"><div><span className="eyebrow">THE STRATEGY EXCHANGE</span><h1>Research worth comparing.<br />Strategies you can own.</h1><p>Compare the evidence. Buy a strategy license. Set your price and list it for resale.</p></div>
      <div className="market-wallet"><Wallet size={22} /><span>Virtual-credit wallet</span><strong>{wallet.data ? credits(wallet.data.balance) : "Loading…"}</strong><label>Demo identity<select aria-label="Marketplace account" value={account} onChange={e => { setAccount(e.target.value); setMessage(""); }}>{(catalog.data?.accounts || [{ account_id: "demo-buyer" }, { account_id: "demo-seller" }]).map(a => <option key={a.account_id}>{a.account_id}</option>)}</select></label></div>
    </div>
    <p className="market-disclosure">Local demo · virtual credits · 100 transferable licenses per strategy. License prices are seller asks; strategy returns describe backtests, not license returns. Demo identities are shared and unauthenticated.</p>
    {(catalog.error || wallet.error || action.error) && <Notice error>{catalog.error || wallet.error || action.error}</Notice>}
    {message && <Notice>{message}</Notice>}
    <div className="market-toolbar"><div className="market-tabs" role="tablist" aria-label="Marketplace sections">{[["discover", "Discover"], ["owned", "My licenses"], ["activity", "Trade history"]].map(([v,t]) => <button role="tab" aria-selected={tab === v} key={v} onClick={() => setTab(v)}>{t}</button>)}</div><button disabled={action.pending} onClick={() => void action.send("/v1/marketplace/sync", {}, false)}><RefreshCw size={14} /> Publish latest research</button></div>
    {tab === "discover" && <>
      <div className="market-controls"><label className="market-search"><Search size={17} /><input aria-label="Search marketplace" placeholder="Search strategy, agent or symbol" value={query} onChange={e => setQuery(e.target.value)} /></label><label>Evaluation<select aria-label="Evaluation period" value={split} onChange={e => setSplit(e.target.value as Split)}>{Object.entries(label).map(([v,t]) => <option key={v} value={v}>{t}</option>)}</select></label><label>Sort by<select aria-label="Sort marketplace" value={sort} onChange={e => setSort(e.target.value)}><option value="sharpe">Highest Sharpe</option><option value="return">Highest return</option><option value="drawdown">Lowest MDD</option><option value="price">Lowest ask</option></select></label></div>
      <div className="market-nike"><div className="market-nike-icon"><FlaskConical size={24} /></div><div><span className="eyebrow">ALTERNATIVE DATA LAB / NKE</span><h2>Nike Review Radar</h2><p>Read the customer, track sentiment deterioration, and inspect the evidence behind every signal.</p></div><button onClick={() => setReviewOpen(true)}>Open review lab <ArrowUpRight size={16} /></button></div>
      {!filtered.length ? <Empty>{catalog.loading ? "Loading strategy exchange…" : "No matching listings. Publish a research run or clear your search."}</Empty> : <div className="strategy-grid market-grid">{filtered.map(a => {
        const m = a.metrics[split], ask = available(a)[0], owned = wallet.data?.holdings.find(h => h.asset_id === a.asset_id)?.quantity || 0;
        return <article className="market-card" key={a.asset_id}><div className="card-top"><span className="market-family"><BarChart3 size={17} />{a.universe.join(" · ")}</span><span className={`market-badge ${a.status === "research" ? "experimental" : ""}`}>{a.status === "research" ? "Unbacktested" : a.gates.length ? "Gates failed" : "Evaluated"}</span></div>
          <button className="market-title" onClick={() => setDetail(a)}><h2>{a.title}</h2><ArrowUpRight size={15} /></button><p className="market-agent">{a.agent_id.replaceAll("_", " ")} · {a.dataset_id || "Review research"}</p>
          <div className="market-stats"><Metric label="Net return" value={pct(m?.net_return)} /><Metric label="Sharpe" value={num(m?.sharpe)} /><Metric label="Max drawdown" value={pct(m?.max_drawdown)} /></div>
          <p className="market-period">{label[split]} · {period(a, split)}<br />{m ? `${m.n_fills} fills · fees ${m.fees_usd.toFixed(2)} · slippage ${m.slippage_usd.toFixed(2)} (dataset quote units)` : "No measured trading performance"}</p>
          <div className="market-card-bottom"><div><span className="muted">Lowest ask</span><strong>{ask ? credits(ask.price) : "No offers"}</strong><small>{ask ? `${available(a).reduce((n,o) => n + o.remaining, 0)} licenses offered` : "Check back later"}{owned ? ` · ${owned} owned` : ""}</small></div><button className="primary" disabled={!ask || !wallet.data || !!wallet.error} onClick={() => setTicket(a)}><ShoppingBag size={15} />Buy</button></div>
          <label className="market-compare"><input type="checkbox" checked={selected.includes(a.asset_id)} disabled={!selected.includes(a.asset_id) && selected.length >= 4} onChange={e => select(e.target.checked ? [...selected, a.asset_id] : selected.filter(s => s !== a.asset_id))} />Compare {a.title}</label>
        </article>;
      })}</div>}
      {!!comparison.length && <section className="market-comparison"><div className="section-title"><h2>Compare strategies <span className="muted">{comparison.length}/4 · {label[split]}</span></h2><button onClick={() => select([])}>Clear comparison</button></div>
        {new Set(comparison.map(a => JSON.stringify([a.dataset_hash, a.periods?.[split]]))).size > 1 && <Notice>Different datasets or evaluation periods. These results are not directly comparable.</Notice>}
        <div className="table-scroll"><table><thead><tr><th>Measure</th>{comparison.map(a => <th key={a.asset_id}>{a.title}<small className="block muted">{a.agent_id}</small></th>)}</tr></thead><tbody>
          {[["Net return", (a: Asset) => pct(a.metrics[split]?.net_return)], ["Sharpe ratio", (a: Asset) => num(a.metrics[split]?.sharpe)], ["Maximum drawdown", (a: Asset) => pct(a.metrics[split]?.max_drawdown)], ["Fills", (a: Asset) => a.metrics[split]?.n_fills ?? "Unavailable"], ["Fees (quote units)", (a: Asset) => num(a.metrics[split]?.fees_usd)], ["Slippage (quote units)", (a: Asset) => num(a.metrics[split]?.slippage_usd)], ["Dataset", (a: Asset) => a.dataset_id || "Unbacktested"], ["Period", (a: Asset) => period(a, split)], ["Lowest ask", (a: Asset) => available(a)[0] ? credits(available(a)[0].price) : "No offers"]].map(([name, value]) => <tr key={name as string}><th>{name as string}</th>{comparison.map(a => <td key={a.asset_id}>{(value as (a: Asset) => string | number)(a)}</td>)}</tr>)}
        </tbody></table></div><p className="muted">MDD is the largest peak-to-trough equity loss. Recorded Sharpe uses the engine’s fixed 252 × 7 bars/year convention. Held-out results were frozen after validation; no guarantees of future performance.</p>
      </section>}
    </>}
    {tab === "owned" && <><div className="section-title"><h2>Available licenses</h2><span className="muted">Listings hold their inventory in escrow</span></div>{!wallet.data?.holdings.length ? <Empty>No available licenses yet. Buy a listing to start your collection.</Empty> : <div className="market-holdings">{wallet.data.holdings.map(h => <div key={h.asset_id}><BarChart3 size={19} /><div><h3>{h.descriptor.title}</h3><span>{h.quantity} available · {h.descriptor.agent_id}</span></div><button onClick={() => setDetail(h.descriptor)}>View strategy</button><button className="primary" onClick={() => setResell(h.descriptor)}>List for resale</button></div>)}</div>}
      <h2>Your active listings</h2>{!wallet.data?.listings.length ? <Empty>No active sale listings.</Empty> : <div className="table-scroll"><table><thead><tr><th>Strategy</th><th>Remaining</th><th>Price per license</th><th>Action</th></tr></thead><tbody>{wallet.data.listings.map(o => <tr key={o.listing_id}><td>{assets.find(a => a.asset_id === o.asset_id)?.title || o.asset_id}</td><td>{o.remaining}</td><td>{credits(o.price)}</td><td><button disabled={action.pending} onClick={() => void action.send(`/v1/marketplace/listings/${o.listing_id}/cancel`, { account_id: account }, false)}>Cancel listing</button></td></tr>)}</tbody></table></div>}</>}
    {tab === "activity" && <><h2>Trade history</h2>{!wallet.data?.trades.length ? <Empty>No completed license trades for this identity.</Empty> : <div className="table-scroll"><table><thead><tr><th>When</th><th>Side</th><th>Strategy</th><th>Counterparty</th><th>Quantity</th><th>Total</th></tr></thead><tbody>{wallet.data.trades.map(t => <tr key={t.request_id}><td>{stamp(t.created_at)}</td><td>{t.buyer_id === account ? "Bought" : "Sold"}</td><td>{assets.find(a => a.asset_id === t.asset_id)?.title || t.asset_id}</td><td>{t.buyer_id === account ? t.seller_id : t.buyer_id}</td><td>{t.quantity}</td><td>{credits(t.total)}</td></tr>)}</tbody></table></div>}</>}
    {detail && <Modal title={detail.title} close={() => setDetail(null)}><p>{detail.description}</p><Notice>{detail.note}</Notice><p><b>{detail.universe.join(", ")}</b> · {detail.agent_id} · {detail.source}</p>{!!detail.gates.length && <Notice error>Research gates failed: {detail.gates.join(", ")}. Inspect the evidence before purchasing.</Notice>}<JsonView label="Frozen metrics, parameters and provenance" value={detail} /><p>One transferable license grants access to the recorded strategy specification in this demo. No exclusive intellectual-property rights or live brokerage execution are included.</p>{detail.family === "review_sentiment" && <button onClick={() => { setDetail(null); setReviewOpen(true); }}>Open Nike review lab</button>}</Modal>}
    {ticket && wallet.data && <BuyTicket key={account + ticket.asset_id} asset={ticket} offers={available(ticket)} account={account} balance={wallet.data.balance} close={() => setTicket(null)} done={() => { changed(); setTicket(null); setMessage("Purchase completed. Your wallet and license holdings have been updated."); }} />}
    {resell && <SellTicket key={account + resell.asset_id} asset={resell} account={account} owned={wallet.data?.holdings.find(h => h.asset_id === resell.asset_id)?.quantity || 0} close={() => setResell(null)} done={() => { changed(); setResell(null); setMessage("Resale listing published. Its licenses are reserved until purchased or canceled."); }} />}
    {reviewOpen && <Modal title="Nike Review Radar / research lab" close={() => setReviewOpen(false)}><ReviewLab /></Modal>}
  </>;
}

function BuyTicket({ asset, offers, account, balance, close, done }: { asset: Asset; offers: Offer[]; account: string; balance: number; close: () => void; done: () => void }) {
  const [listing, setListing] = useState(offers[0]?.listing_id || ""), [quantity, setQuantity] = useState(1), action = useMarketAction(done);
  const offer = offers.find(o => o.listing_id === listing), total = (offer?.price || 0) * quantity;
  const valid = !!offer && Number.isInteger(quantity) && quantity > 0 && quantity <= offer.remaining && total <= balance;
  return <Modal title={`Buy ${asset.title}`} close={close}><form className="market-form" onSubmit={e => { e.preventDefault(); if (valid) void action.send("/v1/marketplace/buy", { account_id: account, listing_id: listing, quantity }); }}><Notice>Pay with demo credits. No real money or stock order is involved.</Notice><label>Seller / asking price<select aria-label="Sale offer" value={listing} onChange={e => setListing(e.target.value)}>{offers.map(o => <option value={o.listing_id} key={o.listing_id}>{o.seller_id} · {credits(o.price)} · {o.remaining} available</option>)}</select></label><label>License quantity<input aria-label="Buy quantity" type="number" min="1" max={offer?.remaining || 1} step="1" value={Number.isNaN(quantity) ? "" : quantity} onChange={e => setQuantity(e.target.valueAsNumber)} required /></label><div className="metrics"><Metric label="Total cost" value={Number.isFinite(total) ? credits(total) : "Unavailable"} /><Metric label="Wallet balance" value={credits(balance)} /></div>{total > balance && <Notice error>Insufficient virtual credits.</Notice>}{action.error && <Notice error>{action.error}</Notice>}<button className="primary" disabled={!valid || action.pending} type="submit">{action.pending ? "Purchasing…" : "Confirm license purchase"}</button></form></Modal>;
}

function SellTicket({ asset, account, owned, close, done }: { asset: Asset; account: string; owned: number; close: () => void; done: () => void }) {
  const [quantity, setQuantity] = useState(1), [price, setPrice] = useState("100.00"), action = useMarketAction(done);
  const valid = Number.isInteger(quantity) && quantity > 0 && quantity <= owned && /^\d+(\.\d{1,2})?$/.test(price) && Number(price) >= .01 && Number(price) <= 100000;
  return <Modal title={`Sell ${asset.title}`} close={close}><form className="market-form" onSubmit={e => { e.preventDefault(); if (valid) void action.send("/v1/marketplace/listings", { account_id: account, asset_id: asset.asset_id, quantity, price_credits: price }); }}><p>{owned} licenses available. Listed licenses enter escrow; cancel to release any unsold inventory.</p><label>Quantity to list<input aria-label="Sell quantity" type="number" min="1" max={owned} step="1" value={Number.isNaN(quantity) ? "" : quantity} onChange={e => setQuantity(e.target.valueAsNumber)} required /></label><label>Asking price per license (virtual credits)<input aria-label="Resale price" type="number" min="0.01" max="100000" step="0.01" value={price} onChange={e => setPrice(e.target.value)} required /></label>{action.error && <Notice error>{action.error}</Notice>}<button className="primary" disabled={!valid || action.pending} type="submit">{action.pending ? "Publishing…" : "Publish resale listing"}</button></form></Modal>;
}

type Analysis = { digest: string; as_of: string; provider: string; model: string; signal: string; recent: { count: number; sentiment: number | null }; baseline: { count: number; sentiment: number | null }; sentiment_change: number | null; limitations: string; method: string; excluded: unknown[]; evidence: { review_id: string; text: string; source_url: string; sentiment: number; themes: string[]; published_at: string; observed_at: string }[] };
function ReviewLab() {
  const [input, setInput] = useState("[]"), [asOf, setAsOf] = useState("2024-06-20T23:59:59Z"), [provider, setProvider] = useState("offline");
  const [result, setResult] = useState<Analysis | null>(null), [error, setError] = useState(""), [pending, setPending] = useState(false), [sample, setSample] = useState(false);
  const history = useResource<{ digest: string; result: Analysis }[]>("/v1/marketplace/reviews/history", {}, 0, result ? 1 : 0);
  async function loadExample() {
    try { const r = await request<{ reviews: unknown[]; as_of: string }>("/v1/marketplace/reviews/example"); setInput(JSON.stringify(r.reviews, null, 2)); setAsOf(r.as_of); setSample(true); setError(""); setResult(null); } catch (e) { setError(errorText(e)); }
  }
  async function run(e: FormEvent) {
    e.preventDefault(); if (pending) return; setPending(true); setError(""); setResult(null);
    try { setResult(await request<Analysis>("/v1/marketplace/reviews/analyze", {}, post({ as_of: asOf, provider, reviews: JSON.parse(input) }, AbortSignal.timeout(120000)))); }
    catch (e) { setError(errorText(e)); } finally { setPending(false); }
  }
  return <div className="review-lab"><p>Compare the last 30 days of product reviews with the preceding 90. Claude or OpenAI reads the text for quality, value and demand themes; the offline baseline scores ratings. A weakening signal means avoid or reduce NKE exposure in a long-only research workflow.</p>
    <details className="json"><summary>Nike case study: what the supplied sources show</summary><p>The July 2024 KOIOS article reports social chatter around an earnings decline, including bot contamination. It describes correlation, without a verified Claude forecast. The AltIndex snapshot describes investor discussions; this is a different signal from customer product reviews.</p><p>The October 2026 news describes an existing selloff and potential shopping-agent competition. It cannot establish a prediction made before that decline. These articles provide context and are not imported as customer reviews or backtest evidence.</p><a href="https://altindex.com/ticker/nke/sentiment" target="_blank" rel="noreferrer">AltIndex investor sentiment ↗</a><br /><a href="https://finance.yahoo.com/technology/ai/articles/nike-down-47-ai-agents-030718620.html" target="_blank" rel="noreferrer">Yahoo / BeInCrypto shopping-agent article ↗</a></details>
    <form className="market-form" onSubmit={run}><div className="row"><button type="button" disabled={pending} onClick={() => void loadExample()}>Load illustrative reviews</button><span className="muted">Or paste your dated review dataset below</span></div>{sample && <Notice>Illustrative synthetic reviews authored for this demo. They are not actual Nike reviews or evidence of a historical prediction.</Notice>}
      <label>Information cutoff (ISO timestamp with timezone)<input aria-label="Review cutoff" value={asOf} onChange={e => { setAsOf(e.target.value); setResult(null); }} required /></label><label>Classifier<select aria-label="Review provider" value={provider} onChange={e => { setProvider(e.target.value); setResult(null); }}><option value="offline">Offline · rating baseline</option><option value="claude">Claude · requires Anthropic key</option><option value="openai">OpenAI · requires OpenAI key</option><option value="auto">Auto · configured provider</option></select></label>
      <label>Review JSON (1–100 records)<textarea aria-label="Review JSON" rows={10} value={input} onChange={e => { setInput(e.target.value); setSample(false); setResult(null); }} spellCheck={false} required /></label><p className="muted">Each record: review_id, source_url, published_at, observed_at, text, rating (1–5). Both timestamps need a timezone. Evidence observed after the cutoff is excluded; duplicate IDs or text count once. Imported provenance is user supplied. Text goes to the selected provider when using Claude or OpenAI.</p>{error && <Notice error>{error}</Notice>}<button className="primary" type="submit" disabled={pending}>{pending ? "Analyzing reviews…" : "Analyze review signal"}</button>
    </form>
    {result && <section className="review-result"><div className="section-title"><h3>{result.signal.replaceAll("_", " ").toUpperCase()}</h3><span className="market-badge">{result.provider} · {result.model}</span></div><p className="muted">Information cutoff: {result.as_of}</p><div className="market-stats"><Metric label={`Recent (${result.recent.count})`} value={num(result.recent.sentiment)} /><Metric label={`Baseline (${result.baseline.count})`} value={num(result.baseline.sentiment)} /><Metric label="Sentiment change" value={num(result.sentiment_change)} /></div><p>{result.method}</p><Notice>{result.limitations}</Notice><div className="review-evidence">{result.evidence.map(r => <div key={r.review_id}><div className="row"><a href={r.source_url} target="_blank" rel="noreferrer">{r.review_id} ↗</a><span className="muted">{num(r.sentiment)} · {r.themes.join(", ")}</span></div><p>{r.text}</p><small>Published {r.published_at} · observed {r.observed_at}</small></div>)}</div><JsonView label="Excluded records and analysis digest" value={{ excluded: result.excluded, digest: result.digest }} /></section>}
    {!!history.data?.length && <JsonView label="Saved analysis history (latest 20)" value={history.data} />}
  </div>;
}
