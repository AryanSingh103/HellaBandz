"use client";
import { useId } from "react";
import { Band, money } from "./model";

export type ChartSeries = {name:string; color:string; values:[number,number][]};

/** Shared chart geometry; the hero formats returns while portfolio charts format cash. */
export function LineChart({series, bands=[], currency="USD", format="money", hero=false}: {
  series:ChartSeries[]; bands?:Band[]; currency?:string; format?:"money"|"percent"; hero?:boolean;
}) {
  const id=useId().replaceAll(":", "");
  const clean=series.map(s=>({...s,values:s.values.filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]))}));
  const points=clean.flatMap(s=>s.values);
  if (!points.length) return <div className="empty">No recorded samples in this context.</div>;
  const W=hero?640:900, H=hero?300:210, left=hero?0:10, right=hero?52:85, top=12, bottom=hero?26:15;
  const xs=points.map(p=>p[0]), ys=points.map(p=>p[1]);
  const xmin=Math.min(...xs), xmax=Math.max(...xs);
  let ymin=Math.min(...ys), ymax=Math.max(...ys);
  if(format==="percent") {
    ymin=Math.min(ymin,0); ymax=Math.max(ymax,0);
    const padding=Math.max(ymax-ymin,.01)*.12; ymin-=padding; ymax+=padding;
  }
  const x=(v:number)=>left+(v-xmin)/Math.max(1,xmax-xmin)*(W-left-right);
  const y=(v:number)=>top+(1-(v-ymin)/Math.max(format==="percent"?.0001:1,ymax-ymin))*(H-top-bottom);
  // Percent axes use round steps (1%, 2.5%, 5%...) so labels read at a glance.
  const step=format==="percent"?(()=>{const raw=(ymax-ymin)/4,mag=10**Math.floor(Math.log10(raw));return [1,2,2.5,5,10].map(m=>m*mag).find(m=>m>=raw)||10*mag;})():0;
  const ticks=step?Array.from({length:Math.floor(ymax/step)-Math.ceil(ymin/step)+1},(_,i)=>(Math.ceil(ymin/step)+i)*step):Array.from({length:5},(_,i)=>ymin+(ymax-ymin)*i/4);
  const label=(v:number)=>format==="percent"?`${(v*100).toFixed(Math.abs(v*100)>=10?0:1)}%`:money(v,currency);
  return <svg className={hero?"kchart":undefined} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio={hero?"none":undefined} role="img" aria-label={series.map(s=>s.name).join(" vs ")}>
    <defs>{clean.map((s,i)=><linearGradient key={s.name} id={`${id}-fill-${i}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={s.color} stopOpacity=".22" /><stop offset="100%" stopColor={s.color} stopOpacity="0" /></linearGradient>)}</defs>
    {bands.filter(b=>b.end>=xmin&&b.start<=xmax).map(b=><g key={b.start} className="band"><rect x={x(Math.max(b.start,xmin))-3} y={top} width={Math.max(8,x(Math.min(b.end,xmax))-x(Math.max(b.start,xmin))+6)} height={H-top-bottom} /><text x={x(Math.max(b.start,xmin))+8} y={top+14}>{b.label}</text></g>)}
    {ticks.map((t,i)=><g key={i}><line x1={left} x2={W-right} y1={y(t)} y2={y(t)} className="grid" /><text x={W-right+10} y={y(t)+4} className="axis">{label(t)}</text></g>)}
    {format==="percent"&&<line x1={left} x2={W-right} y1={y(0)} y2={y(0)} className="zero" />}
    {hero&&Array.from({length:5},(_,i)=>xmin+(xmax-xmin)*i/4).map((t,i)=><text key={i} x={Math.min(x(t),W-right-40)} y={H-6} className="axis">{new Date(t*1000).toLocaleDateString(undefined,{month:"short",day:"numeric"})}</text>)}
    {clean.map((s,i)=>{
      if(!s.values.length)return null;
      const d=s.values.map(([a,b],k)=>`${k?"L":"M"}${x(a).toFixed(1)},${y(b).toFixed(1)}`).join(" ");
      const [lx,ly]=s.values[s.values.length-1];
      return <g key={s.name}>{s.values.length>1&&i===0&&<path d={`${d} L${x(lx)},${H-bottom} L${x(s.values[0][0])},${H-bottom} Z`} fill={`url(#${id}-fill-${i})`} />}<path d={d} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" /><circle cx={x(lx)} cy={y(ly)} r="8" fill={s.color} opacity=".18" /><circle cx={x(lx)} cy={y(ly)} r="3.5" fill={s.color} /></g>;
    })}
  </svg>;
}
