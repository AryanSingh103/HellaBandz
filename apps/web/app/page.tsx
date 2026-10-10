"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";
const usd = (micros: number) => (micros / 1e6).toLocaleString("en-US", { style: "currency", currency: "USD" });
const px = (micros: number) => (micros / 1e6).toFixed(2);
const pct = (x: number) => `${(x * 100).toFixed(2)}%`;
const short = (h?: string | null, n = 12) => (h ? h.slice(0, n) : "—");

const QUEUED = "queued: the worker is down, the command is stored and will run in order when it restarts";

type Any = any; // eslint-disable-line @typescript-eslint/no-explicit-any

async function get(path: string) {
  const r = await fetch(API + path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
async function post(path: string, body: unknown, headers: Record<string, string> = {}) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

function usePoll<T>(path: string | null, ms: number): T | null {
  const [v, setV] = useState<T | null>(null);
  useEffect(() => {
    setV(null); // never show the previous path's data (e.g. another portfolio) while the first fetch is in flight
    if (!path) return;
    let alive = true;
    const tick = () => get(path).then((x) => alive && setV(x)).catch(() => alive && setV(null));
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
  }, [path, ms]);
  return v;
}

function Line({ pts, height = 140 }: { pts: number[]; height?: number }) {
  if (pts.length < 2) return <div className="note">waiting for bars…</div>;
  const w = 600, h = height, lo = Math.min(...pts), hi = Math.max(...pts), rng = hi - lo || 1;
  const d = pts.map((p, i) => `${i ? "L" : "M"}${(i / (pts.length - 1)) * w},${h - ((p - lo) / rng) * (h - 10) - 5}`).join(" ");
  const up = pts[pts.length - 1] >= pts[0];
  const label = (x: number) => x.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  return (
    <div className="chart">
      <span className="hi">{label(hi)}</span>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="equity curve">
        <path d={d} fill="none" stroke={up ? "var(--good)" : "var(--bad)"} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
      </svg>
      <span className="lo">{label(lo)}</span>
    </div>
  );
}

export default function Terminal() {
  const [pid, setPid] = useState("main");
  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get("p");
    if (p) setPid(p);
  }, []);

  const health = usePoll<Any>("/v1/health", 2000);
  const port = usePoll<Any>(`/v1/portfolio?portfolio_id=${pid}`, 1000);
  const equity = usePoll<Any[]>(`/v1/equity?portfolio_id=${pid}&limit=3000`, 2000);
  const orders = usePoll<Any[]>(`/v1/orders?portfolio_id=${pid}&limit=60`, 1500);
  const rejections = usePoll<Any[]>(`/v1/analytics/rejections?portfolio_id=${pid}`, 3000);
  const agentStats = usePoll<Any[]>(`/v1/analytics/agents?portfolio_id=${pid}`, 3000);
  const tours = usePoll<Any[]>("/v1/tournaments?limit=1", 5000);
  const msgs = usePoll<Any[]>("/v1/agent-messages?limit=120", 2000);

  const st = port?.state;
  const tour = tours?.[0];
  const m = tour?.manifest;

  // ---------------------------------------------------------------- event stream (WebSocket)
  const [events, setEvents] = useState<Any[]>([]);
  const opened = port != null;
  const seqRef = useRef(0);
  seqRef.current = port?.seq ?? 0;
  useEffect(() => {
    setEvents([]);
    if (!opened) return;
    let alive = true;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let last = Math.max(0, seqRef.current - 40);
    const connect = () => {
      ws = new WebSocket(API.replace(/^http/, "ws") + `/v1/stream?portfolio_id=${encodeURIComponent(pid)}&after_seq=${last}`);
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type !== "events" || !msg.events.length) return;
        last = msg.events[msg.events.length - 1].seq;
        setEvents((prev) => [...msg.events.reverse(), ...prev].slice(0, 300));
      };
      ws.onclose = () => { if (alive) retry = setTimeout(connect, 1000); }; // API restart: resume after the last seen seq
    };
    connect();
    return () => { alive = false; clearTimeout(retry); ws?.close(); };
  }, [pid, opened]);

  // ---------------------------------------------------------------- verification
  const [verify, setVerify] = useState<Any>(null);
  const runVerify = useCallback(async () => setVerify(await get(`/v1/verify?portfolio_id=${pid}`).catch(() => null)), [pid]);
  useEffect(() => setVerify(null), [pid]);

  // ---------------------------------------------------------------- adversarial actions
  const [adv, setAdv] = useState<string>("");
  const ctx = useMemo(() => {
    if (!st) return null;
    const agents = Object.keys(st.allocations ?? {}).filter((a) => st.allocations[a] > 0);
    const syms = Object.keys(st.last_bar ?? {}).sort();
    if (!agents.length || !syms.length) return null;
    const sym = syms[0];
    return { agent: agents[0], sym, ts: new Date(st.last_bar[sym][0] * 1000).toISOString(), close: st.last_bar[sym][1] };
  }, [st]);

  async function retry100() {
    if (!ctx) return;
    const order = { client_order_id: `ui-retry-${Date.now()}`, agent_id: ctx.agent, symbol: ctx.sym, side: "BUY", qty: 1, signal_bar_ts: ctx.ts };
    setAdv("sending the same order 100× concurrently…");
    const rs = await Promise.all(Array.from({ length: 100 }, () => post(`/v1/orders?portfolio_id=${pid}`, order)));
    const fresh = rs.filter((r) => r.body.duplicate_request === false).length;
    if (rs[0].body.outcome === undefined) {
      setAdv(`100 concurrent requests for ${order.client_order_id}\n→ ${fresh} new command, ${100 - fresh} recognized as duplicates\n→ HTTP ${rs[0].status} ${QUEUED}`);
      return;
    }
    const qtys = new Set(rs.map((r) => r.body.approved_qty));
    setAdv(`100 concurrent requests for ${order.client_order_id}\n→ ${fresh} new command, ${100 - fresh} recognized as duplicates\n→ distinct decisions returned: ${JSON.stringify([...qtys])} (outcome ${rs[0].body.outcome})\nOne economic effect; check the event inspector: a single OrderAccepted.`);
  }
  async function single(label: string, o: Any) {
    const r = await post(`/v1/orders?portfolio_id=${pid}`, o);
    const b = r.body;
    const verdict = b.outcome ? `${b.outcome}${b.rules?.length ? ` (${b.rules.join(", ")})` : ""}: approved ${b.approved_qty} of ${o.qty}` : r.status === 202 ? QUEUED : "";
    setAdv(`${label}\n→ HTTP ${r.status} ${verdict}\n${JSON.stringify(b)}`);
  }
  const oversized = () => ctx && single("Oversized buy (100,000 shares)", { client_order_id: `ui-big-${Date.now()}`, agent_id: ctx.agent, symbol: ctx.sym, side: "BUY", qty: 100000, signal_bar_ts: ctx.ts });
  const stale = () => ctx && single("Order based on day-old data", { client_order_id: `ui-stale-${Date.now()}`, agent_id: ctx.agent, symbol: ctx.sym, side: "BUY", qty: 1, signal_bar_ts: new Date(Date.parse(ctx.ts) - 86400000).toISOString() });
  const rogue = () => ctx && single("Unallocated agent tries to trade", { client_order_id: `ui-rogue-${Date.now()}`, agent_id: "rogue_agent", symbol: ctx.sym, side: "BUY", qty: 1, signal_bar_ts: ctx.ts });
  const short_ = () => ctx && single("Short sale attempt (long-only book)", { client_order_id: `ui-short-${Date.now()}`, agent_id: ctx.agent, symbol: ctx.sym, side: "SELL", qty: 1000000, signal_bar_ts: ctx.ts });
  async function kill(on: boolean) {
    const r = await post(`/v1/admin/kill-switch?portfolio_id=${pid}`, { on, reason: "operator" });
    setAdv(`kill switch ${on ? "ON" : "OFF"}\n→ HTTP ${r.status} ${r.status === 202 ? QUEUED : r.body.status ?? ""}\n${JSON.stringify(r.body)}`);
  }

  // ---------------------------------------------------------------- ask / voice
  const [q, setQ] = useState("Why was the last order rejected?");
  const [ans, setAns] = useState<Any>(null);
  const [listening, setListening] = useState(false);
  async function ask(question: string) {
    setAns({ answer: "thinking…" });
    const r = await post("/v1/ask", { question, portfolio_id: pid });
    setAns(r.body);
    const text = r.body.answer as string;
    try {
      const a = await fetch(API + "/v1/speak", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) });
      if (a.ok) { new Audio(URL.createObjectURL(await a.blob())).play(); return; }
    } catch { /* fall through to browser speech */ }
    if ("speechSynthesis" in window) window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
  function listen() {
    const SR = (window as Any).SpeechRecognition || (window as Any).webkitSpeechRecognition;
    if (!SR) { setAns({ answer: "Speech recognition is not available in this browser; type the question instead." }); return; }
    const rec = new SR();
    rec.lang = "en-US";
    rec.onresult = (e: Any) => { const t = e.results[0][0].transcript; setQ(t); ask(t); };
    rec.onend = () => setListening(false);
    setListening(true);
    rec.start();
  }

  // ---------------------------------------------------------------- derived views
  const eqPts = useMemo(() => (equity ?? []).slice().reverse().map((e) => e.equity / 1e6), [equity]);
  const positions = useMemo(() => {
    if (!st) return [];
    return Object.entries(st.positions ?? {})
      .filter(([, p]: Any) => p.qty > 0)
      .map(([k, p]: Any) => {
        const [agent, sym] = k.split("|");
        const last = st.last_bar?.[sym]?.[1] ?? 0;
        return { agent, sym, qty: p.qty, reserved: p.reserved, basis: p.basis, value: p.qty * last, upnl: p.qty * last - p.basis };
      });
  }, [st]);
  const workerOk = health?.worker_alive;

  return (
    <>
      <header className="top">
        <h1>QUANTSWARM</h1>
        <span className="tag">autonomous strategy agents · deterministic execution · paper trading only</span>
        <span className="pill"><span className="dot" style={{ background: health ? "var(--good)" : "var(--bad)" }} />API</span>
        <span className={`pill ${health && !workerOk ? "bad" : ""}`}><span className="dot" style={{ background: workerOk ? "var(--good)" : "var(--bad)" }} />execution worker {workerOk ? "up" : "DOWN"}</span>
        <span className="pill">pending commands <b className="mono">{health?.pending_commands ?? "—"}</b></span>
        <span className="pill">portfolio <input value={pid} onChange={(e) => setPid(e.target.value)} style={{ width: 90, padding: "1px 6px" }} /></span>
        <span className="pill mono">seq {port?.seq ?? "—"} · digest {short(port?.digest)}</span>
        {st?.kill_switch && <span className="pill bad">KILL SWITCH ON ({st.kill_reason})</span>}
      </header>

      <main className="grid">
        <section className="panel span-8">
          <h2>Portfolio <span className="muted mono">{port ? new Date(port.updated_at).toLocaleTimeString() : ""}</span></h2>
          <div className="kpis">
            <div className="kpi"><div className="l">Equity</div><div className="v">{port ? usd(port.equity) : "—"}</div></div>
            <div className="kpi"><div className="l">Cash</div><div className="v">{st ? usd(st.cash) : "—"}</div></div>
            <div className="kpi"><div className="l">Reserved</div><div className="v">{st ? usd(st.reserved_cash) : "—"}</div></div>
            <div className="kpi"><div className="l">Exposure</div><div className="v">{st && port ? pct(st.market_value / port.equity) : "—"}</div></div>
            <div className="kpi"><div className="l">Realized P&L</div><div className={`v ${st?.realized_pnl >= 0 ? "good" : "bad"}`}>{st ? usd(st.realized_pnl) : "—"}</div></div>
            <div className="kpi"><div className="l">Fees + slippage</div><div className="v">{st ? usd(st.fees_paid + st.slippage_cost) : "—"}</div></div>
          </div>
          {health && !port && <p className="note">Portfolio “{pid}” is not open yet. Run <code>make session</code>.</p>}
          <div style={{ marginTop: 10 }}><Line pts={eqPts} /></div>
        </section>

        <section className="panel span-4">
          <h2>Ledger integrity <button onClick={runVerify}>Replay &amp; verify</button></h2>
          <p className="note">The API independently replays the hash-chained event log with the same pure engine and compares the digest to the worker's stored projection.</p>
          {verify && (
            <div className="result">
              <div className={verify.match ? "good" : "bad"}>{verify.match ? "✔ digests match" : "✘ MISMATCH"} · chain {verify.chain_ok ? "intact" : "BROKEN"}</div>
              events replayed: {verify.events}{"\n"}replayed: {short(verify.replayed_digest, 24)}{"\n"}stored:   {short(verify.stored_digest, 24)}{"\n"}head:     {short(verify.head_hash, 24)}
            </div>
          )}
          <h2 style={{ marginTop: 14 }}>Allocations</h2>
          <table><tbody>
            {st && Object.entries(st.allocations ?? {}).map(([a, c]: Any) => (
              <tr key={a}><td>{a}</td><td className="num">{usd(c)}</td></tr>
            ))}
          </tbody></table>
        </section>

        <section className="panel span-7">
          <h2>Strategy tournament {tour && <span className="mono muted">{tour.run_id} · manifest {short(tour.manifest_hash)}</span>}</h2>
          {!m && <div className="note">No tournament yet. Run <code>make tournament</code>.</div>}
          {m && (
            <>
              <div className="scroll"><table>
                <thead><tr><th>agent</th><th>family</th><th>src</th><th className="num">train SR</th><th className="num">val SR</th><th className="num">val DD</th><th className="num">stab.</th><th className="num">score</th><th className="num">weight</th><th>gates</th></tr></thead>
                <tbody>{m.ranked.map((r: Any) => {
                  const src = m.proposals.find((p: Any) => p.agent_id === r.agent_id)?.source;
                  return (
                    <tr key={r.strategy_id}>
                      <td>{r.agent_id}</td><td className="mono">{r.family}</td><td className="muted">{src}</td>
                      <td className="num">{r.train.sharpe.toFixed(2)}</td><td className="num">{r.validation.sharpe.toFixed(2)}</td>
                      <td className="num">{pct(r.validation.max_drawdown)}</td><td className="num">{r.stability.toFixed(2)}</td>
                      <td className="num">{r.score.toFixed(3)}</td><td className="num">{pct(m.allocations.by_strategy[r.strategy_id].weight)}</td>
                      <td className="bad" style={{ whiteSpace: "normal" }}>{r.gates.join("; ")}</td>
                    </tr>);
                })}</tbody>
              </table></div>
              <p className="note">
                Held-out test (evaluated once, after allocations froze): return <b className="mono">{pct(m.held_out_test.portfolio.metrics.net_return)}</b>,
                Sharpe <b className="mono">{m.held_out_test.portfolio.metrics.sharpe.toFixed(2)}</b>, max DD <b className="mono">{pct(m.held_out_test.portfolio.metrics.max_drawdown)}</b>.
                Dataset <span className="mono">{m.dataset.dataset_id}</span> ({short(m.dataset.content_sha256)}).
                {tour.solana_sig ? <> On-chain commitment: <a href={`https://explorer.solana.com/tx/${tour.solana_sig}?cluster=devnet`} target="_blank">{short(tour.solana_sig, 16)}</a></> : " Not yet committed to Solana."}
              </p>
              <p className="note"><b>Supervisor ({m.supervisor.source}):</b> {m.supervisor.rationale}</p>
            </>
          )}
        </section>

        <section className="panel span-5">
          <h2>Agent channel</h2>
          <div className="scroll" style={{ maxHeight: 360 }}>
            {(msgs ?? []).map((x: Any) => (
              <div className="msg" key={x.id}>
                <span className="who">{x.agent_id}</span><span className="role">{x.role} · {new Date(x.created_at).toLocaleTimeString()}</span>
                <pre>{summarizeMsg(x)}</pre>
              </div>
            ))}
          </div>
        </section>

        <section className="panel span-6">
          <h2>Positions</h2>
          <div className="scroll"><table>
            <thead><tr><th>agent</th><th>symbol</th><th className="num">qty</th><th className="num">reserved</th><th className="num">value</th><th className="num">unrealized</th></tr></thead>
            <tbody>{positions.map((p) => (
              <tr key={p.agent + p.sym}><td>{p.agent}</td><td>{p.sym}</td><td className="num">{p.qty}</td><td className="num">{p.reserved}</td>
                <td className="num">{usd(p.value)}</td><td className={`num ${p.upnl >= 0 ? "good" : "bad"}`}>{usd(p.upnl)}</td></tr>
            ))}</tbody>
          </table></div>
          {!positions.length && <div className="note">No open positions.</div>}
          <h2 style={{ marginTop: 12 }}>Working orders</h2>
          <table><tbody>{st && Object.values(st.orders ?? {}).map((o: Any) => (
            <tr key={o.order_id}><td className="mono">{o.order_id.slice(-28)}</td><td>{o.side}</td><td className="num">{o.remaining}/{o.qty}</td><td className="num">{o.bars_seen} bars</td></tr>
          ))}</tbody></table>
          {!Object.keys(st?.orders ?? {}).length && <div className="note">None. Approved orders fill on the next bar.</div>}
        </section>

        <section className="panel span-6">
          <h2>Order decisions (risk engine)</h2>
          <div className="scroll"><table>
            <thead><tr><th>agent</th><th>sym</th><th>side</th><th className="num">req</th><th className="num">appr</th><th className="num">filled</th><th>outcome</th><th>status</th><th>rules</th></tr></thead>
            <tbody>{(orders ?? []).map((o: Any) => (
              <tr key={o.order_id}>
                <td>{o.agent_id}</td><td>{o.symbol}</td><td>{o.side}</td><td className="num">{o.requested_qty}</td><td className="num">{o.approved_qty}</td><td className="num">{o.filled_qty}</td>
                <td><span className={`badge b-${o.outcome}`}>{o.outcome}</span></td><td className="muted">{o.status}</td><td className="mono muted">{o.rules.join(",")}</td>
              </tr>
            ))}</tbody>
          </table></div>
        </section>

        <section className="panel span-4">
          <h2>Adversarial console</h2>
          <p className="note">Everything here goes through the public API and the same risk engine the agents use.</p>
          <div className="row">
            <button onClick={retry100} disabled={!ctx}>Same order ×100 (concurrent)</button>
            <button onClick={oversized} disabled={!ctx}>Oversized order</button>
            <button onClick={stale} disabled={!ctx}>Stale-data order</button>
            <button onClick={rogue} disabled={!ctx}>Unallocated agent</button>
            <button onClick={short_} disabled={!ctx}>Short sale</button>
            <button className="danger" onClick={() => kill(true)}>Kill switch ON</button>
            <button onClick={() => kill(false)}>Kill switch OFF</button>
          </div>
          {adv && <div className="result">{adv}</div>}
          <p className="note" style={{ marginTop: 10 }}>Crash test: <code>make crash-worker</code>, keep submitting (commands queue, HTTP 202), then <code>make restart-worker</code> and press “Replay &amp; verify”.</p>
        </section>

        <section className="panel span-4">
          <h2>Ask QuantSwarm</h2>
          <div className="row">
            <input style={{ flex: 1, minWidth: 0 }} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask(q)} />
            <button onClick={() => ask(q)}>Ask</button>
            <button onClick={listen} disabled={listening}>{listening ? "listening…" : "🎤"}</button>
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            {["Which agent performed best?", "What is our current exposure?", "Why was the last order rejected?"].map((s) => (
              <button key={s} onClick={() => { setQ(s); ask(s); }} style={{ fontSize: 11 }}>{s}</button>
            ))}
          </div>
          {ans && <div className="result">{ans.answer}{ans.mode && <div className="muted" style={{ marginTop: 6 }}>mode: {ans.mode} · read-only, grounded in live DB rows</div>}</div>}
        </section>

        <section className="panel span-4">
          <h2>Risk analytics (TigerData)</h2>
          <table><thead><tr><th>rule</th><th className="num">orders</th></tr></thead>
            <tbody>{(rejections ?? []).map((r: Any) => <tr key={r.rule}><td className="mono">{r.rule}</td><td className="num">{r.n}</td></tr>)}</tbody></table>
          <table style={{ marginTop: 10 }}><thead><tr><th>agent</th><th className="num">appr</th><th className="num">red.</th><th className="num">rej.</th><th className="num">filled</th></tr></thead>
            <tbody>{(agentStats ?? []).map((a: Any) => <tr key={a.agent_id}><td>{a.agent_id}</td><td className="num">{a.approved}</td><td className="num">{a.reduced}</td><td className="num">{a.rejected}</td><td className="num">{a.filled_qty}</td></tr>)}</tbody></table>
        </section>

        <section className="panel span-12">
          <h2>Event inspector (live, hash-chained) <span className="muted mono">{events.length} shown</span></h2>
          <div className="scroll" style={{ maxHeight: 280 }}><table>
            <thead><tr><th className="num">seq</th><th>type</th><th>details</th><th>hash</th><th>prev</th></tr></thead>
            <tbody>{events.map((e: Any) => (
              <tr key={e.seq}><td className="num">{e.seq}</td><td>{e.type}</td><td className="mono muted" style={{ whiteSpace: "normal" }}>{eventDetail(e)}</td>
                <td className="mono">{short(e.hash, 10)}</td><td className="mono muted">{short(e.prev_hash, 10)}</td></tr>
            ))}</tbody>
          </table></div>
        </section>
      </main>
    </>
  );
}

function eventDetail(e: Any): string {
  switch (e.type) {
    case "BarApplied": return `${e.symbol} close ${px(e.close)} @ ${new Date(e.ts * 1000).toISOString().slice(0, 16)}`;
    case "OrderAccepted": return `${e.agent_id} ${e.side} ${e.qty}/${e.requested_qty} ${e.symbol} ${e.outcome} reserve ${usd(e.reserved_cash)} ${e.rules.join(",")}`;
    case "OrderRejected": return `${e.agent_id} ${e.side} ${e.requested_qty} ${e.symbol} ${e.rules.join(",")}`;
    case "OrderFilled": return `${e.agent_id} ${e.side} ${e.qty} ${e.symbol} @ ${px(e.px)} fee ${usd(e.fee)}`;
    case "OrderClosed": return `${e.order_id} (${e.reason})`;
    case "KillSwitchSet": return `${e.on ? "ON" : "OFF"} (${e.reason})`;
    case "AllocationSet": return `${e.agent_id} ${usd(e.capital)}`;
    case "PortfolioOpened": return `initial cash ${usd(e.initial_cash)}`;
    default: return JSON.stringify(e).slice(0, 160);
  }
}

function summarizeMsg(x: Any): string {
  const c = x.content ?? {};
  switch (x.role) {
    case "proposal": return `[${c.source}] ${c.proposal?.family} on ${c.proposal?.universe?.join(",")}\n${c.proposal?.rationale ?? ""}`;
    case "fit": return `best of ${c.candidates} on train: ${JSON.stringify(c.chosen_params)} (SR ${c.train_metrics?.sharpe?.toFixed?.(2)})`;
    case "validation": return `validation SR ${c.validation?.sharpe?.toFixed(2)}, DD ${(c.validation?.max_drawdown * 100).toFixed(1)}%, stability ${c.stability?.toFixed(2)} → score ${c.score?.toFixed(3)} ${c.gates?.length ? "✘ " + c.gates.join("; ") : "✔"}`;
    case "allocation_advice": return `[${c.source}] ${c.rationale}`;
    case "crisis_response": return `[${c.decision?.source}] ${c.decision?.action} ${c.decision?.fraction ?? ""}: ${c.decision?.rationale}\n` + (c.orders ?? []).map((o: Any) => `  ${o.side} ${o.qty} ${o.symbol} → ${o.outcome} ${o.approved_qty ?? ""} ${o.rules?.join(",") ?? ""}`).join("\n");
    case "order_reject": case "order_reduce": return `${c.side} ${c.qty} ${c.symbol} → approved ${c.approved_qty} (${c.rules?.join(",")})`;
    case "allocations": return Object.entries(c.allocations ?? {}).map(([k, v]: Any) => `${k.split(".").pop()} ${(v.weight * 100).toFixed(1)}% = ${usd(v.capital_micros)}`).join("\n");
    case "held_out_test": return `held-out test, evaluated once: return ${pct(c.portfolio?.net_return ?? 0)}, Sharpe ${c.portfolio?.sharpe?.toFixed(2)}, max DD ${pct(c.portfolio?.max_drawdown ?? 0)}, ${c.portfolio?.n_fills} fills`;
    case "session_start": return `replaying ${c.steps} hourly steps at ${c.speed_steps_per_s}/s` + (c.crash_start_step != null ? `; crash scheduled at step ${c.crash_start_step}` : "");
    case "market_crash": return `⚠ ${c.overlay} at ${c.ts}`;
    case "session_summary": return `steps ${c.steps}, orders ${c.orders} (appr ${c.approved}, red ${c.reduced}, rej ${c.rejected}, dup retries ${c.duplicates}), equity ${c.final_equity_usd?.toFixed?.(2)}, verify ${c.verify?.match}`;
    default: return JSON.stringify(c).slice(0, 300);
  }
}
