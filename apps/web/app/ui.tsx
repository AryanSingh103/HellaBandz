"use client";
import { ReactNode, useEffect, useRef } from "react";
import { Inbox, X } from "lucide-react";
import { money } from "./model";
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
export function Chart({ series, currency = "USD" }: { series: { name: string; color: string; values: [number, number][] }[]; currency?: string }) {
  const all = series.flatMap(s => s.values).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (!all.length) return <Empty>No recorded samples in this context.</Empty>;
  const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
  const x = (v: number) => 10 + (v - xmin) / Math.max(1, xmax - xmin) * 800;
  const y = (v: number) => 195 - (v - ymin) / Math.max(1, ymax - ymin) * 175;
  const ticks = [0, 1, 2, 3, 4].map(i => ymin + (ymax - ymin) * i / 4);
  return <div className="chart"><div className="chart-legend">{series.map(s => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}</div>
    <svg viewBox="0 0 900 210" role="img" aria-label={series.map(s => s.name).join(", ")}>
      <defs>{series.map((s, i) => <linearGradient key={i} id={`cfill${i}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={s.color} stopOpacity=".2" /><stop offset="100%" stopColor={s.color} stopOpacity="0" /></linearGradient>)}</defs>
      {ticks.map((t, i) => <g key={i}><line x1="10" x2="815" y1={y(t)} y2={y(t)} className="grid" /><text x="825" y={y(t) + 4} className="axis">{money(t, currency)}</text></g>)}
      {series.map((s, i) => { const pts = s.values.filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1])); if (!pts.length) return null;
        const d = pts.map(([a, b], k) => `${k ? "L" : "M"}${x(a)},${y(b)}`).join(" "); const [lx, ly] = pts[pts.length - 1];
        return <g key={s.name}>{i === 0 && pts.length > 1 && <path d={`${d} L${x(lx)},195 L${x(pts[0][0])},195 Z`} fill={`url(#cfill${i})`} />}
          <path d={d} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
          <circle cx={x(lx)} cy={y(ly)} r="8" fill={s.color} opacity=".2" /><circle cx={x(lx)} cy={y(ly)} r="3.5" fill={s.color} /></g>; })}</svg>
    <div className="chart-ends"><span>{new Date(xmin * 1000).toLocaleDateString()}</span><span>{new Date(xmax * 1000).toLocaleDateString()}</span></div></div>;
}
