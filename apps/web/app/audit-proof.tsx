"use client";
import { useState } from "react";
import { useResource } from "./data";
import { errorText, request, stamp, url } from "./model";
import { JsonView, Metric, Notice } from "./ui";

type Status={configured:boolean;signer:string;coverage:string[];pending_changes:number;latest:null|{anchor_id:string;signature:string;manifest:{created_at:string;tables:Record<string,{count:number;sha256:string}>}};service:null|{alive:boolean;error?:string};request:null|{completed_at?:string;error?:string}};
type Proof={ok:boolean;status:string;error?:string;pending_changes?:number;sealed_records?:number;explorer?:string;sealed_at?:string};
export function AuditProof(){
  const [revision,setRevision]=useState(0),[proof,setProof]=useState<Proof|null>(null),[pending,setPending]=useState(false),[error,setError]=useState("");
  const status=useResource<Status>("/v1/audit/status",{},10000,revision),s=status.data;
  async function verify(){setPending(true);setError("");try{setProof(await request<Proof>("/v1/audit/verify",{},{method:"POST",signal:AbortSignal.timeout(180000)}));}catch(e){setError(errorText(e));}finally{setPending(false);setRevision(n=>n+1);}}
  async function seal(){setPending(true);setError("");try{await request("/v1/audit/request",{},{method:"POST"});setRevision(n=>n+1);}catch(e){setError(errorText(e));}finally{setPending(false);}}
  return <section><div className="section-title"><h2>Solana audit seal</h2><span className="badge">Devnet / test SOL</span></div>
    <p>One blockchain commitment seals trades, agent messages, research and strategy sales together. Verify reads the latest finalized proof from the trusted wallet and compares the underlying records.</p>
    {(error||status.error)&&<Notice error>{error||status.error}</Notice>}
    <div className="metrics"><Metric label="Last seal" value={stamp(s?.latest?.manifest.created_at)}/><Metric label="Unsealed changes" value={s?.pending_changes??"Unknown"}/><Metric label="Audit service" value={!s?"Unknown":s.service?.alive?"Running":"Offline"}/><Metric label="Record sets covered" value={s?.coverage.length??"Unknown"}/></div>
    {s?.service?.error&&<Notice error>{s.service.error}</Notice>}
    {s?.request&&!s.request.completed_at&&<Notice>Seal requested. The signer batches requests at its next interval.</Notice>}
    {s?.request?.error&&<Notice error>{s.request.error}</Notice>}
    <div className="row"><button disabled={pending||!s?.configured} onClick={verify}>{pending?"Working...":"Verify blockchain & records"}</button><button disabled={pending||!s?.service?.alive||!!s.request&&!s.request.completed_at} onClick={seal}>Request new seal</button>
      {s?.latest&&<><a target="_blank" rel="noreferrer" href={`https://explorer.solana.com/tx/${s.latest.signature}?cluster=devnet`}>Open Solana transaction</a><a href={url(`/v1/audit/anchors/${s.latest.anchor_id}`)}>Download receipt</a></>}
    </div>
    {proof&&<Notice error={!proof.ok}>{proof.ok?`Verified ${proof.sealed_records} sealed records. ${proof.pending_changes} later changes await their next seal.`:`${proof.status}: ${proof.error||"Verification failed"}`}</Notice>}
    <p className="muted">Changes after the seal remain provisional. This proves record integrity, not strategy accuracy or trade profitability. The marketplace transfers virtual license credits.</p>
    {s&&<JsonView label="Coverage and trusted public wallet" value={{signer:s.signer,tables:s.latest?.manifest.tables||s.coverage}}/>}
    {proof&&<JsonView label="Independent verification details" value={proof}/>}
  </section>;
}
