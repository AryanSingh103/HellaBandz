"use client";
import { useEffect, useRef, useState } from "react";
import { ApiError, errorText, request, id, post, url, Event, HistoryPage } from "./model";

export function useResource<T>(path: string | null, params: Record<string, string | number | undefined> = {}, interval = 0, revision = 0, nullable = false) {
  const key = JSON.stringify([path, params]);
  const [result, setResult] = useState<{ key: string; data?: T | null; error?: string; time?: number; loading: boolean }>({ key, loading: true });
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const load = async () => {
      if (!path) { setResult({ key, loading: false }); return; }
      try {
        const data = await request<T>(path, params, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]) });
        if (active) setResult({ key, data, time: Date.now(), loading: false });
      } catch (e) {
        if (active) {
          if (nullable && e instanceof ApiError && e.status === 404) setResult({ key, data: null, time: Date.now(), loading: false });
          else setResult(old => ({ key, data: old.key === key ? old.data : undefined, time: old.key === key ? old.time : undefined, error: errorText(e), loading: false }));
        }
      } finally { if (active && interval) timer = setTimeout(load, interval); }
    };
    void load();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
    // The serialized key captures all query parameters and prevents old-scope data being rendered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, interval, revision, nullable]);
  return result.key === key ? result : { key, loading: true, data: undefined, error: undefined, time: undefined };
}

export function useCommand(pid?: string) {
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const retry = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => { controller.current = new AbortController(); return () => controller.current?.abort(); }, []);
  async function send(path: string, body: unknown, params: Record<string, string> = {}) {
    if (pending || !controller.current || controller.current.signal.aborted) return null;
    const signature = JSON.stringify([path, body, params]);
    if (retry.current?.signature !== signature) retry.current = { signature, key: id("cmd") };
    setPending(true); setError(""); setResult(null);
    try {
      const r = await request<Record<string, unknown>>(path, { ...(pid ? { portfolio_id: pid } : {}), ...params }, post(body, AbortSignal.any([controller.current.signal, AbortSignal.timeout(45000)]), retry.current.key));
      if (controller.current.signal.aborted) return null;
      setResult(r); retry.current = null; return r;
    } catch(e) { if (!controller.current.signal.aborted) setError(errorText(e)); return null; }
    finally { if (!controller.current.signal.aborted) setPending(false); }
  }
  return { send, pending, error, result };
}

export function useEventStream(pid: string | null) {
  const [state,setState]=useState<{pid:string|null;status:string;events:Event[]}>({pid,status:"connecting",events:[]});
  useEffect(()=>{
    let active=true, socket:WebSocket|null=null, timer:ReturnType<typeof setTimeout>,last=0;
    if (!pid) { setState({pid,status:"paused",events:[]}); return; }
    setState({pid,status:"connecting",events:[]});
    const controller=new AbortController();
    const merge=(events:Event[])=>{
      if(!active)return;
      events=events.filter(e=>Number.isInteger(e.seq)&&typeof e.type==="string");
      last=Math.max(last,...events.map(e=>e.seq));
      setState(old=>({pid,status:"connected",events:Array.from(new Map([...(old.pid===pid?old.events:[]),...events].map(e=>[e.seq,e])).values()).sort((a,b)=>b.seq-a.seq).slice(0,100)}));
    };
    const connect=()=>{
      if(!active)return;
      const u=new URL(url("/v1/stream",{portfolio_id:pid,after_seq:last}));u.protocol=u.protocol==="https:"?"wss:":"ws:";
      socket=new WebSocket(u);socket.onopen=()=>{if(active)setState(old=>({pid,status:"connected",events:old.pid===pid?old.events:[]}));};
      socket.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.type==="events"&&Array.isArray(m.events))merge(m.events);}catch{if(active)setState(old=>({...old,status:"invalid stream response"}));}};
      socket.onerror=()=>{if(active)setState(old=>({...old,status:"disconnected"}));};
      socket.onclose=()=>{if(active){setState(old=>({...old,status:"reconnecting"}));timer=setTimeout(connect,2000);}};
    };
    void request<HistoryPage>("/v1/history/events",{portfolio_id:pid,limit:40},{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)])}).then(r=>{if(active)merge(r.items.map(row=>row.event));}).catch(()=>{}).finally(connect);
    return()=>{active=false;controller.abort();clearTimeout(timer);socket?.close();};
  },[pid]);
  return state.pid===pid?state:{pid,status:"connecting",events:[]};
}
