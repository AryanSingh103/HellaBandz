"use client";
import { useEffect, useRef, useState } from "react";
import { Send, RotateCcw } from "lucide-react";
import { errorText, post, request } from "./model";
import { Notice } from "./ui";

export function Ask({pid}:{pid:string}) {
  const [question,setQuestion]=useState("Why was the last order rejected?"),[pending,setPending]=useState(false),[answer,setAnswer]=useState<{answer:string;mode?:string}|null>(null),[error,setError]=useState("");
  const active=useRef(false),controller=useRef<AbortController|null>(null);
  useEffect(()=>{active.current=true;return()=>{active.current=false;controller.current?.abort();};},[]);
  async function ask(text=question) {
    if(pending)return;const q=text.trim();
    if(!q||q.length>500){setError("Enter a nonempty question of no more than 500 characters.");return;}
    setPending(true);setError("");setAnswer(null);controller.current?.abort();const c=new AbortController();controller.current=c;
    try {const a=await request<{answer?:unknown;mode?:string}>("/v1/ask",{},post({question:q,portfolio_id:pid},AbortSignal.any([c.signal,AbortSignal.timeout(45000)])));if(typeof a.answer!=="string"||!a.answer.trim())throw new Error("The answer provider returned an invalid response. Retry your question.");if(active.current&&!c.signal.aborted)setAnswer({answer:a.answer,mode:a.mode});}
    catch(e){if(active.current&&!c.signal.aborted)setError(errorText(e));}
    finally{if(active.current&&!c.signal.aborted)setPending(false);}
  }
  return <><p className="muted">Read-only answers / Portfolio {pid}</p><form onSubmit={e=>{e.preventDefault();void ask();}}><label>Question<textarea aria-label="Ask question" rows={3} value={question} onChange={e=>setQuestion(e.target.value)} /></label><div className="row"><button className="primary" disabled={pending}><Send size={16} />{pending?"Thinking...":"Ask"}</button><span className={question.trim().length>500?"bad":"muted"}>{question.trim().length} / 500</span></div></form>
    <div className="suggestions">{["Why was the last order rejected?","What is our current exposure?","Which agent performed best?"].map(q=><button disabled={pending} key={q} onClick={()=>{setQuestion(q);void ask(q);}}>{q}</button>)}</div>{error&&<Notice error>{error}<button disabled={pending} onClick={()=>ask()}><RotateCcw size={14} />Retry</button></Notice>}{answer&&<div className="answer"><p>{answer.answer}</p><p className="muted">{answer.mode||"Grounded answer"} / {pid}</p></div>}
  </>;
}
