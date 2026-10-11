"use client";
import { ReactNode, useEffect, useRef } from "react";
import { Inbox, X } from "lucide-react";
import { Band, Metrics, pct } from "./model";
import { LineChart, ChartSeries } from "./charts";
import { Term } from "./help";

export function Empty({ children }: { children: ReactNode }) { return <div className="empty"><Inbox size={25} aria-hidden="true" /><div>{children}</div></div>; }
export function JsonView({ value, label = "Recorded details" }: { value: unknown; label?: string }) {
  return <details className="json"><summary>{label}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}
export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) { return <div className={`notice ${error ? "error" : ""}`} role={error ? "alert" : "status"}>{children}</div>; }
export function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onClick={close}><section ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()} onKeyDown={e => {
    if (e.key === "Escape") close();
    if (e.key === "Tab") { const nodes = ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input,select,textarea,summary'); if (!nodes?.length) return; const first = nodes[0], last = nodes[nodes.length-1]; if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); } }
  }}>
    <div className="section-title"><h2>{title}</h2><button autoFocus className="icon" title="Close details" aria-label="Close details" onClick={close}><X size={20} /></button></div>{children}</section></div>;
}
export function Metric({ label, value }: { label: string; value: ReactNode }) { return <div className="metric"><span><Term k={label}>{label}</Term></span><strong className={typeof value === "string" && /^-[$\d]/.test(value) ? "neg" : undefined}>{value}</strong></div>; }
export function PerformanceMetrics({metrics, prefix="", fills=false}: {metrics?: Metrics | null; prefix?: string; fills?: boolean}) {
  return <><Metric label={`${prefix}Net return`} value={pct(metrics?.net_return)} /><Metric label={`${prefix}Sharpe`} value={metrics?.sharpe?.toFixed(2) ?? "Unavailable"} /><Metric label={`${prefix}Max drawdown`} value={pct(metrics?.max_drawdown)} />{fills && <Metric label={`${prefix}Fills`} value={metrics?.n_fills ?? "Unavailable"} />}</>;
}
export function Chart({ series, currency = "USD", bands = [] }: { series: ChartSeries[]; currency?: string; bands?: Band[] }) {
  const points = series.flatMap(s=>s.values).filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]));
  if (!points.length) return <Empty>No recorded samples in this context.</Empty>;
  const dates=points.map(p=>p[0]);
  const date=(t:number)=>new Date(t*1000).toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"});
  return <div className="chart"><div className="chart-legend">{series.map(s=><span key={s.name}><i style={{background:s.color}} />{s.name}</span>)}</div><LineChart series={series} currency={currency} bands={bands} /><div className="chart-ends"><span>{date(Math.min(...dates))}</span>{bands.length>0&&<span className="band-note">Shaded: scripted crash injected to test the risk engine (not a real market event)</span>}<span>{date(Math.max(...dates))}</span></div></div>;
}
