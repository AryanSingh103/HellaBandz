"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, RotateCcw, Send, X, Search, ListFilter, Play } from "lucide-react";
import { Dataset, Event, HistoryPage, Order, Portfolio, Replay, State, errorText, id, money, post, request, stamp } from "./model";
import { useCommand, useResource } from "./data";
import { Empty, JsonView, Metric, Modal, Notice } from "./ui";

type Filters = { order_id:string; agent_id:string; symbol:string; status:string; outcome:string; event_type:string; from_seq:string; to_seq:string; from_time:string; to_time:string };
const initialFilters: Filters = {order_id:"",agent_id:"",symbol:"",status:"",outcome:"",event_type:"",from_seq:"",to_seq:"",from_time:"",to_time:""};
type Ticket = {client_order_id:string;agent_id:string;symbol:string;side:string;qty:number;order_type:string;limit_px?:number;signal_bar_ts:string};
function baseQuantity(input:string,lots:number) {
  if(!/^\d+(\.\d+)?$/.test(input)) return null;
  const [whole,fraction=""] = input.split("."), scale = 10n ** BigInt(fraction.length);
  const numerator = (BigInt(whole)*scale+BigInt(fraction||"0"))*BigInt(lots);
  if(numerator%scale!==0n || numerator/scale>BigInt(Number.MAX_SAFE_INTEGER)) return null;
  const n=Number(numerator/scale);return n>0?n:null;
}
export function OrdersView({pid,port,dataset,orderId,selectOrder,refresh}:{pid:string;port?:Portfolio|null;dataset?:Dataset;orderId:string;selectOrder:(s:string)=>void;refresh:()=>void}) {
  const state=port?.state, agents=Object.keys(state?.allocations||{}), symbols=Object.keys(state?.last_bar||{});
  const [agent,setAgent]=useState(""),[symbol,setSymbol]=useState(""),[side,setSide]=useState("BUY"),[type,setType]=useState("MKT"),[quantity,setQuantity]=useState("1"),[limit,setLimit]=useState("");
  const [error,setError]=useState(""),[submitted,setSubmitted]=useState<Ticket|null>(null),[revision,revise]=useState(0);
  const c=useCommand(pid),lots=dataset?.lots_per_base_unit||1,currency=dataset?.quote_currency||"USD";
  const selectedAgent=agents.includes(agent)?agent:agents[0]||"", selectedSymbol=symbols.includes(symbol)?symbol:symbols[0]||"";
  const bar=state?.last_bar[selectedSymbol];
  const commandSeq=typeof c.result?.command_seq==="number"?c.result.command_seq:undefined;
  const command=useResource<{status:string;response:Record<string,unknown>;error?:string}>(commandSeq?`/v1/commands/${commandSeq}`:null,{portfolio_id:pid},2000);
  async function send(retry=false) {
    let ticket=submitted;
    if(!retry) {
      const qty=baseQuantity(quantity,lots), price=type==="LMT"?Number(limit)*1e6/lots:undefined;
      if(!qty||!bar||!selectedAgent||price!==undefined&&(!Number.isSafeInteger(price)||price<=0)){setError(`Enter a positive quantity in ${lots>1?"base units":"whole shares"}${type==="LMT"?" and a positive, representable limit price":""}.`);return;}
      ticket={client_order_id:id("order"),agent_id:selectedAgent,symbol:selectedSymbol,side,qty,order_type:type,signal_bar_ts:new Date(bar[0]*1000).toISOString(),...(price===undefined?{}:{limit_px:price})};
      setSubmitted(ticket);
    }
    if(!ticket)return;setError("");await c.send("/v1/orders",ticket);revise(n=>n+1);refresh();
  }
  return <><div className="section-title"><h1>Orders</h1><span className="muted">{pid} / Simulated execution</span></div>
    <section className="order-ticket"><form onSubmit={e=>{e.preventDefault();void send();}}><div className="section-title"><h2>Order ticket</h2><span className="muted">{bar?`Simulation: ${stamp(bar[0])}`:"No applied market bars"}</span></div><div className="ticket-grid">
      <div><span className="field-label">Side</span><div className="segmented" aria-label="Order side">{["BUY","SELL"].map(v=><button type="button" aria-pressed={side===v} key={v} className={side===v?"active":""} onClick={()=>setSide(v)}>{v==="BUY"?"Buy":"Sell"}</button>)}</div></div>
      <div><span className="field-label">Order type</span><div className="segmented" aria-label="Order type">{["MKT","LMT"].map(v=><button type="button" aria-pressed={type===v} key={v} className={type===v?"active":""} onClick={()=>setType(v)}>{v==="MKT"?"Market":"Limit"}</button>)}</div></div>
      <label>Agent<select aria-label="Order agent" value={selectedAgent} onChange={e=>setAgent(e.target.value)}>{!agents.length&&<option>No allocated agents</option>}{agents.map(a=><option key={a}>{a}</option>)}</select></label>
      <label>Symbol<select aria-label="Order symbol" value={selectedSymbol} onChange={e=>setSymbol(e.target.value)}>{!symbols.length&&<option>No eligible symbols</option>}{symbols.map(a=><option key={a}>{a}</option>)}</select></label>
      <label>Quantity ({lots>1?"base units":"shares"})<input aria-label="Order quantity" inputMode="decimal" value={quantity} onChange={e=>setQuantity(e.target.value)} /></label>
      {type==="LMT"&&<label>Limit ({currency} / {lots>1?"base unit":"share"})<input aria-label="Limit price" inputMode="decimal" value={limit} onChange={e=>setLimit(e.target.value)} /></label>}
      <button className="primary" disabled={!port||!bar||!selectedAgent||c.pending}><Send size={16} />{c.pending?"Submitting...":"Submit order"}</button>
    </div></form><div className="row ticket-meta"><span>Last close: {bar?money(bar[1]*lots,currency):"Unavailable"}</span><span>{lots>1?`${lots.toLocaleString()} fixed lots per base unit`:"Whole-share quantities"}</span>{state?.kill_switch&&<span className="bad">Kill switch active</span>}</div>
      {(error||c.error)&&<Notice error>{error||c.error}</Notice>}{submitted&&<div className="request-result"><div className="row"><span className="mono">Request {submitted.client_order_id}</span><button disabled={c.pending} onClick={()=>send(true)}><RotateCcw size={14} />Retry same request</button><button onClick={()=>selectOrder(submitted.client_order_id)}>Open lifecycle</button></div>{c.result&&<JsonView label={`Decision: ${String(command.data?.response?.outcome||c.result.outcome||c.result.status||"processed")}`} value={command.data?.response||c.result} />}{command.error&&<Notice error>{command.error}</Notice>}{command.data?.status==="pending"&&<Notice>Queued: awaiting the selected execution worker.</Notice>}</div>}
    </section>
    <History key={`${pid}:${revision}`} pid={pid} dataset={dataset} selectOrder={selectOrder} />
    {orderId&&<OrderDetail key={orderId} pid={pid} orderId={orderId} dataset={dataset} close={()=>selectOrder("")} />}
  </>;
}

export function History({pid,initialCollection="orders",dataset,selectOrder}:{pid:string;initialCollection?:"orders"|"fills"|"events";dataset?:Dataset;selectOrder?:(s:string)=>void}) {
  const [collection,setCollection]=useState(initialCollection),[draft,setDraft]=useState(initialFilters),[filters,setFilters]=useState(initialFilters);
  const [cursor,setCursor]=useState<number|undefined>(),[snapshot,setSnapshot]=useState<number|undefined>(),[stack,setStack]=useState<(number|undefined)[]>([]),[revision,revise]=useState(0),[replaySeq,setReplaySeq]=useState<number|null>(null);
  const [localOrder,setLocalOrder]=useState("");
  const params:Record<string,string|number|undefined>={portfolio_id:pid,cursor,snapshot_seq:snapshot,limit:40,...filters,
    from_time:filters.from_time?new Date(filters.from_time).toISOString():undefined,to_time:filters.to_time?new Date(filters.to_time).toISOString():undefined};
  const data=useResource<HistoryPage>(`/v1/history/${collection}`,params,0,revision);
  useEffect(()=>{if(data.data&&snapshot===undefined)setSnapshot(data.data.snapshot_seq);},[data.data,snapshot]);
  function reset(){setCursor(undefined);setSnapshot(undefined);setStack([]);revise(n=>n+1);}
  const lots=dataset?.lots_per_base_unit||1,currency=dataset?.quote_currency||"USD";
  return <section><div className="section-title"><div className="tabs" aria-label="History collection">{(["orders","fills","events"] as const).map(v=><button className={collection===v?"active":""} aria-pressed={collection===v} key={v} onClick={()=>{setCollection(v);reset();}}>{v}</button>)}</div><button className="icon" title="Refresh history snapshot" aria-label="Refresh history snapshot" onClick={reset}><RotateCcw size={16} /></button></div>
    <form className="history-filters" onSubmit={e=>{e.preventDefault();setFilters(draft);reset();}}><label className="search-field"><Search size={16} /><input aria-label="Order ID search" placeholder="Order ID" value={draft.order_id} onChange={e=>setDraft({...draft,order_id:e.target.value})} /></label><details className="filter-details"><summary><ListFilter size={15} />Filters</summary><div className="form-grid">
      <label>Agent<input aria-label="History agent" value={draft.agent_id} onChange={e=>setDraft({...draft,agent_id:e.target.value})} /></label><label>Symbol<input aria-label="History symbol" value={draft.symbol} onChange={e=>setDraft({...draft,symbol:e.target.value})} /></label>
      {collection==="orders"&&<><label>Status<select aria-label="History status" value={draft.status} onChange={e=>setDraft({...draft,status:e.target.value})}><option value="">All</option>{["working","filled","cancelled","expired","rejected"].map(v=><option key={v}>{v}</option>)}</select></label><label>Outcome<select aria-label="History outcome" value={draft.outcome} onChange={e=>setDraft({...draft,outcome:e.target.value})}><option value="">All</option>{["APPROVE","REDUCE","REJECT"].map(v=><option key={v}>{v}</option>)}</select></label></>}
      <label>Event type<input aria-label="History event type" value={draft.event_type} onChange={e=>setDraft({...draft,event_type:e.target.value})} placeholder="OrderFilled" /></label>{(["from_seq","to_seq"] as const).map(k=><label key={k}>{k==="from_seq"?"From sequence":"Through sequence"}<input type="number" min="0" aria-label={k} value={draft[k]} onChange={e=>setDraft({...draft,[k]:e.target.value})} /></label>)}{(["from_time","to_time"] as const).map(k=><label key={k}>{k==="from_time"?"From time":"To time"}<input type="datetime-local" aria-label={k} value={draft[k]} onChange={e=>setDraft({...draft,[k]:e.target.value})} /></label>)}</div></details><button type="submit">Apply</button><button type="button" onClick={()=>{setFilters(initialFilters);setDraft(initialFilters);reset();}}>Clear</button></form>
    <p className="muted">Snapshot through sequence {data.data?.snapshot_seq??"..."}{data.time?` / ${new Date(data.time).toLocaleTimeString()}${data.error?" (stale)":""}`:""}</p>{data.error&&<Notice error>{data.error}</Notice>}
    <div className="table-scroll"><table><thead>{collection==="orders"?<tr><th>Order</th><th>Agent / Symbol</th><th>Side / Type</th><th>Requested</th><th>Approved / Filled</th><th>Outcome</th><th>Status</th><th>Replay</th></tr>:<tr><th>Seq</th><th>Type</th><th>Order / Agent</th><th>Symbol / Quantity</th><th>Details / Fees</th><th>Replay</th></tr>}</thead><tbody>{data.data?.items.map(row=>collection==="orders"?<tr key={row.order_id}><td><button className="link" onClick={()=>selectOrder?selectOrder(row.order_id):setLocalOrder(row.order_id)}>{row.order_id}</button></td><td>{row.agent_id}<br/><span className="muted">{row.symbol}</span></td><td>{row.side} / {row.order_type}</td><td>{row.requested_qty/lots}</td><td>{row.approved_qty/lots} / {row.filled_qty/lots}</td><td><span className={`badge ${row.outcome==="REJECT"?"bad":row.outcome==="REDUCE"?"warn":"good"}`}>{row.outcome}</span></td><td>{row.status}</td><td><button className="icon" title={`Replay event ${row.cursor_seq}`} aria-label={`Replay event ${row.cursor_seq}`} onClick={()=>setReplaySeq(row.cursor_seq)}><Play size={15} /></button></td></tr>:<tr key={row.seq}><td>{row.seq}</td><td>{row.type}</td><td>{row.event.order_id&&<button className="link" onClick={()=>selectOrder?selectOrder(row.event.order_id!):setLocalOrder(row.event.order_id!)}>{row.event.order_id}</button>}<br/>{row.event.agent_id}</td><td>{row.event.symbol} {row.event.qty!==undefined?row.event.qty/lots:""}</td><td>{row.event.px!==undefined?money(row.event.px*lots,currency):""}{row.event.fee!==undefined?` / ${money(row.event.fee,currency)} fee`:""}<JsonView label="Event & hash" value={row} /></td><td><button className="icon" title={`Replay event ${row.seq}`} aria-label={`Replay event ${row.seq}`} onClick={()=>setReplaySeq(row.seq)}><Play size={15} /></button></td></tr>)}</tbody></table></div>
    {!data.data?.items.length&&<Empty>{data.loading?"Loading history...":"No records match the selected portfolio and filters."}</Empty>}
    <div className="pagination"><button disabled={!stack.length||data.loading} onClick={()=>{setCursor(stack[stack.length-1]);setStack(stack.slice(0,-1));}}><ArrowLeft size={15} />Newer</button><span>Page {stack.length+1}</span><button disabled={data.data?.next_cursor==null||data.loading} onClick={()=>{setStack([...stack,cursor]);setCursor(data.data!.next_cursor!);}} >Older<ArrowRight size={15} /></button></div>
    {replaySeq!==null&&<ReplayView pid={pid} seq={replaySeq} currency={currency} close={()=>setReplaySeq(null)} />}{localOrder&&<OrderDetail pid={pid} orderId={localOrder} dataset={dataset} close={()=>setLocalOrder("")} />}
  </section>;
}

function OrderDetail({pid,orderId,dataset,close}:{pid:string;orderId:string;dataset?:Dataset;close:()=>void}) {
  const [revision,revise]=useState(0),[seq,setSeq]=useState<number|null>(null);
  const data=useResource<{order:Order|null;commands:unknown[];events:{seq:number;type:string;event:Event}[]}>(`/v1/orders/${encodeURIComponent(orderId)}`,{portfolio_id:pid},2000,revision),c=useCommand(pid);
  return <Modal title="Order lifecycle" close={close}><p className="mono">{orderId}</p>{data.error&&<Notice error>{data.error}</Notice>}{data.data&&<><JsonView label="Order request & decision" value={data.data.order} /><JsonView label="Durable commands" value={data.data.commands} />{data.data.order?.status==="working"&&<button disabled={c.pending} onClick={async()=>{await c.send(`/v1/orders/${encodeURIComponent(orderId)}/cancel`,{},{agent_id:data.data!.order!.agent_id});revise(n=>n+1);}}><X size={16} />Cancel working order</button>}{c.error&&<Notice error>{c.error}</Notice>}{c.result&&<JsonView label="Cancellation response" value={c.result} />}{data.data.events.map(e=><div className="lifecycle-event" key={e.seq}><div className="row"><b>#{e.seq} {e.type}</b><button onClick={()=>setSeq(e.seq)}><Play size={14} />Replay through event</button></div><JsonView label="Event, fees & hashes" value={e.event} /></div>)}{!data.data.events.length&&<Notice>Request recorded. Awaiting execution events.</Notice>}</>}{seq!==null&&<ReplayView pid={pid} seq={seq} currency={dataset?.quote_currency||"USD"} close={()=>setSeq(null)} />}</Modal>;
}
function ReplayView({pid,seq,currency,close}:{pid:string;seq:number;currency:string;close:()=>void}) {
  const data=useResource<Replay>(`/v1/replay/${seq}`,{portfolio_id:pid});
  return <Modal title={`Read-only replay / Event ${seq}`} close={close}>{data.loading&&<Notice>Reconstructing state through event {seq}...</Notice>}{data.error&&<Notice error>{data.error}</Notice>}{data.data&&<><progress value={data.data.progress} max={data.data.total} /><p>Replayed {data.data.progress} / {data.data.total} events. Live ledger unchanged.</p><div className="metrics"><Metric label="Equity before" value={money(data.data.before.equity,currency)} /><Metric label="Equity after" value={money(data.data.after.equity,currency)} /><Metric label="Cash change" value={money(data.data.after.cash-data.data.before.cash,currency)} /><Metric label="Fee change" value={money(data.data.after.fees_paid-data.data.before.fees_paid,currency)} /></div><JsonView label="Event and full before / after state" value={data.data} /></>}</Modal>;
}
