"use client";
import { useRef, useState } from "react";
import { useResource } from "./data";
import { errorText, post, request, stamp } from "./model";
import styles from "./account.module.css";

type Environment = "demo" | "production";
type Connections = { connections: { environment: Environment; key_hint: string; verified_at: string }[]; credential_storage_ready: boolean; demo_orders_enabled: boolean };
type Snapshot = { balance: { balance?: number; balance_dollars?: string; portfolio_value?: number }; positions: unknown; orders: unknown };
type Market = { ticker: string; title?: string; yes_ask_dollars?: string; status?: string };
type Attempt = { client_order_id: string; status: string; created_at: string; payload: { ticker: string; count: string; price: string } };
const dollars = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);

export function KalshiAccount({ account }: { account: { email: string; default_portfolio_id: string } }) {
  const [environment, setEnvironment] = useState<Environment>("demo");
  const [revision, setRevision] = useState(0), [pending, setPending] = useState(false);
  const [apiKey, setApiKey] = useState(""), [pem, setPem] = useState("");
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const [ticker, setTicker] = useState(""), [count, setCount] = useState("1"), [price, setPrice] = useState("10"), [confirmed, setConfirmed] = useState(false);
  const [orderResult, setOrderResult] = useState<unknown>(null), [orderUncertain, setOrderUncertain] = useState(false);
  const orderId = useRef<string | null>(null);
  const connections = useResource<Connections>("/v1/account/kalshi", {}, 0, revision);
  const attempts = useResource<Attempt[]>("/v1/account/kalshi/demo-orders", {}, 0, revision);
  const unresolved = attempts.data?.filter(a => a.status === "unknown" || a.status === "submitting") || [];
  const connection = connections.data?.connections.find(c => c.environment === environment);
  const snapshot = useResource<Snapshot>(connection ? "/v1/account/kalshi/snapshot" : null, { environment }, 0, revision);
  const markets = useResource<{ markets: Market[] }>(connection ? "/v1/account/kalshi/markets" : null, { environment }, 0, revision);
  async function perform(action: () => Promise<void>) {
    if (pending) return;
    setPending(true); setError(""); setMessage("");
    try { await action(); } catch (e) { setError(errorText(e)); }
    finally { setPending(false); }
  }
  async function connect() {
    await request("/v1/account/kalshi", {}, { ...post({ environment, api_key: apiKey.trim(), private_key: pem.trim() }), method: "PUT" });
    setPem(""); setApiKey(""); setMessage(`${environment === "demo" ? "Demo" : "Production"} credentials verified and encrypted on the server.`); setRevision(n => n + 1);
  }
  async function disconnect() {
    await request("/v1/account/kalshi", { environment }, { method: "DELETE" });
    setMessage("Connection removed. You can also revoke its API key in Kalshi."); setRevision(n => n + 1);
  }
  async function placeDemoOrder() {
    if (!confirmed || environment !== "demo" || orderUncertain) return;
    orderId.current ||= crypto.randomUUID();
    try {
      const result = await request("/v1/account/kalshi/demo-orders", {}, post({ ticker: ticker.trim(), count: Number(count), yes_price_cents: Number(price), client_order_id: orderId.current, acknowledge_demo_order: true }));
      setOrderResult(result); setConfirmed(false); setOrderUncertain(false); orderId.current = null;
      setMessage("Demo exchange accepted the order request. Inspect fill_count below; an IOC order may fill partially or not at all."); setRevision(n => n + 1);
    } catch (e) {
      setOrderUncertain(true); setConfirmed(false); setRevision(n => n + 1);
      throw e;
    }
  }
  const premium = Number(count) * Number(price) / 100;
  const rawBalance = snapshot.data?.balance;
  const balance = rawBalance?.balance_dollars !== undefined ? Number(rawBalance.balance_dollars) : rawBalance?.balance !== undefined ? rawBalance.balance / 100 : undefined;
  return <main className={styles.page}><div className={styles.actions} style={{ justifyContent: "space-between" }}><div><span className={styles.badge}>Private account</span><h1>Account & Kalshi</h1></div><button disabled={pending} onClick={() => setRevision(n => n + 1)}>Refresh exchange data</button></div><p className="muted" style={{ marginTop: 12 }}>{account.email}</p>
    <div className={styles.notice}>Your research workspace uses virtual capital. Kalshi balances below come from the selected exchange environment. Production access is read-only. The strategy swarm does not place Kalshi orders automatically.</div>
    {(error || connections.error) && <p className={`${styles.notice} ${styles.error}`} role="alert">{error || connections.error}</p>}{message && <p className={styles.notice} role="status">{message}</p>}
    <div className={styles.grid}><section className={styles.panel}><h2>Exchange connection</h2><label>Environment<select value={environment} disabled={pending} onChange={e => { setEnvironment(e.target.value as Environment); setError(""); setMessage(""); setOrderResult(null); setConfirmed(false); setPem(""); setApiKey(""); }}><option value="demo">Kalshi demo · test funds</option><option value="production">Kalshi production · real account, read-only</option></select></label><p className="muted" style={{ marginTop: 14 }}>Demo and production use different API keys. Generate your key in the corresponding Kalshi account.</p>
      {connection && <p className={styles.notice}>Connected · key ending {connection.key_hint}<br />Verified {stamp(connection.verified_at)}</p>}
      {connections.data && !connections.data.credential_storage_ready && <p className={`${styles.notice} ${styles.error}`}>Server credential encryption needs configuration before connecting an exchange.</p>}
      <form className={styles.form} onSubmit={e => { e.preventDefault(); void perform(connect); }}><label>Kalshi API key ID<input required autoComplete="off" spellCheck={false} value={apiKey} onChange={e => setApiKey(e.target.value)} disabled={pending} /></label><label>Kalshi private key (PEM)<textarea className={styles.secret} required rows={7} autoComplete="off" spellCheck={false} placeholder="-----BEGIN PRIVATE KEY-----" value={pem} onChange={e => setPem(e.target.value)} disabled={pending} /></label><p className="muted" style={{ fontSize: 12, margin: 0 }}>The API verifies the key with Kalshi and stores it encrypted. It is never returned to the browser. Use HTTPS for deployment.</p><button className="primary" disabled={pending || !connections.data?.credential_storage_ready}>{pending ? "Working..." : connection ? "Verify & replace connection" : "Verify & connect"}</button></form>{connection && <button style={{ marginTop: 12 }} disabled={pending} onClick={() => void perform(disconnect)}>Disconnect {environment}</button>}
    </section><section className={styles.panel}><span className={styles.badge}>{environment === "demo" ? "Demo funds" : "Real Kalshi account · read-only"}</span><h2>Available exchange balance</h2>{snapshot.error && <p className={`${styles.notice} ${styles.error}`} role="alert">{snapshot.error}</p>}<div className={styles.balance}>{connection ? balance !== undefined && Number.isFinite(balance) ? dollars(balance) : snapshot.loading ? "Loading..." : "Unavailable" : "Connect to view"}</div><p className="muted">{environment === "demo" ? "Test funds on Kalshi's demo exchange." : "Actual account balance reported by Kalshi. This app cannot submit production orders."}</p>{snapshot.data && <><details><summary>Exchange positions</summary><pre className={styles.detail}>{JSON.stringify(snapshot.data.positions, null, 2)}</pre></details><details style={{ marginTop: 12 }}><summary>Recent exchange orders</summary><pre className={styles.detail}>{JSON.stringify(snapshot.data.orders, null, 2)}</pre></details></>}<hr style={{ border: 0, borderTop: "1px solid var(--line)", margin: "24px 0" }} /><h2>Execution status</h2><p>Authenticated account reads and market discovery are available after connection. Manual demo orders require the server demo flag. Production order execution and autonomous Kalshi strategies are pending.</p><p className="muted">Research portfolio: {account.default_portfolio_id}<br />Research capital is simulated and does not represent this exchange balance.</p></section></div>
    {connection && <section className={styles.panel} style={{ marginTop: 24 }}><h2>Open Kalshi markets · {environment}</h2>{markets.error && <p className={`${styles.notice} ${styles.error}`} role="alert">{markets.error}</p>}{markets.loading && <p role="status">Loading markets...</p>}{markets.data?.markets.map(m => <div className={styles.market} key={m.ticker}><div className={styles.actions} style={{ justifyContent: "space-between" }}><div><strong>{m.title || m.ticker}</strong><div className="muted">{m.ticker}{m.yes_ask_dollars ? ` · YES ask $${m.yes_ask_dollars}` : ""}</div></div>{environment === "demo" && connections.data?.demo_orders_enabled && <button disabled={pending || orderUncertain || unresolved.length > 0} onClick={() => { setTicker(m.ticker); setConfirmed(false); }}>Use ticker</button>}</div></div>)}{markets.data && !markets.data.markets.length && <p>No open markets were returned by this exchange.</p>}</section>}
    {environment === "demo" && connection && <section className={styles.panel} style={{ marginTop: 24 }}><span className={styles.badge}>Demo exchange only</span><h2>Test an actual Kalshi demo order</h2><p>Buy whole YES contracts with a limit price and immediate-or-cancel execution. Premium is capped at $5 per request before exchange fees. Test funds only.</p>{!connections.data?.demo_orders_enabled ? <p className="muted">Manual demo orders are disabled by the server.</p> : <form className={styles.form} onSubmit={e => { e.preventDefault(); void perform(placeDemoOrder); }}><label>Market ticker<input required value={ticker} onChange={e => { setTicker(e.target.value); setConfirmed(false); }} disabled={pending || orderUncertain || unresolved.length > 0} /></label><div className={styles.actions}><label>YES contracts<input required type="number" min="1" max="100" step="1" value={count} onChange={e => { setCount(e.target.value); setConfirmed(false); }} disabled={pending || orderUncertain || unresolved.length > 0} /></label><label>Limit price (cents)<input required type="number" min="1" max="99" step="1" value={price} onChange={e => { setPrice(e.target.value); setConfirmed(false); }} disabled={pending || orderUncertain || unresolved.length > 0} /></label></div><p>Maximum premium: {Number.isFinite(premium) ? dollars(premium) : "Invalid amount"} before fees.</p><label className="toggle"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} disabled={pending || orderUncertain || unresolved.length > 0} />I confirm this manual demo purchase uses test funds.</label><button className="primary" disabled={pending || !confirmed || orderUncertain || unresolved.length > 0 || !Number.isInteger(Number(count)) || !Number.isInteger(Number(price)) || premium <= 0 || premium > 5}>Submit demo YES order</button></form>}{orderUncertain && <p className={`${styles.notice} ${styles.error}`}>The attempt was not confirmed. Refresh and inspect your demo orders before another request.<button onClick={() => { setRevision(n => n + 1); }}>Refresh exchange orders</button><button onClick={() => { orderId.current = null; setOrderUncertain(false); setError(""); }}>I inspected orders · start a new request</button></p>}{orderResult !== null && <pre className={styles.detail}>{JSON.stringify(orderResult, null, 2)}</pre>}</section>}
    {!!attempts.data?.length && <section className={styles.panel} style={{ marginTop: 24 }}><h2>Persisted demo order attempts</h2><p className="muted">These records survive signout and page reload. An unknown outcome blocks further demo orders until you inspect the exchange orders and acknowledge the uncertainty.</p>{attempts.data.map(attempt => <div className={styles.market} key={attempt.client_order_id}><strong>{attempt.payload.ticker} · {attempt.status}</strong><p className="muted">{attempt.payload.count} YES at ${attempt.payload.price} · {stamp(attempt.created_at)}<br />Client order ID: {attempt.client_order_id}</p>{["unknown", "submitting"].includes(attempt.status) && <button disabled={pending || (attempt.status === "submitting" && Date.now() - new Date(attempt.created_at).getTime() < 60000)} onClick={() => void perform(async () => { await request(`/v1/account/kalshi/demo-orders/${attempt.client_order_id}/acknowledge`, {}, post({ acknowledge_exchange_orders_reviewed: true })); orderId.current = null; setOrderUncertain(false); setConfirmed(false); setRevision(n => n + 1); setMessage("Uncertain attempt acknowledged. The old request will never be resent. A new request is a separate order."); })}>I inspected exchange orders · acknowledge uncertainty</button>}</div>)}</section>}{attempts.error && <p className={`${styles.notice} ${styles.error}`}>{attempts.error}</p>}
  </main>;
}
