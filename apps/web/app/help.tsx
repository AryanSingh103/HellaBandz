"use client";
import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { BookOpen, ChevronDown, X, FlaskConical, Scale, Radio, ShieldCheck, Briefcase, ListOrdered, BarChart3, Play, MessageCircle, Store } from "lucide-react";
import { Manifest, View } from "./model";

/* ------------------------------------------------------------------ hints state (localStorage, SSR-safe) */

type Hints = { on: boolean; setOn: (v: boolean) => void; dismissed: string[]; dismiss: (k: string) => void; reset: () => void };
const HintsContext = createContext<Hints>({ on: true, setOn: () => {}, dismissed: [], dismiss: () => {}, reset: () => {} });
const KEY = "qs.hints.v1";

function read(): { on: boolean; dismissed: string[] } {
  try { const v = JSON.parse(localStorage.getItem(KEY) || "null"); if (v && typeof v.on === "boolean" && Array.isArray(v.dismissed)) return v; } catch { /* storage unavailable */ }
  return { on: true, dismissed: [] };
}
function write(v: { on: boolean; dismissed: string[] }) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* storage unavailable */ } }

export function HintsProvider({ children }: { children: ReactNode }) {
  const [s, setS] = useState<{ on: boolean; dismissed: string[] }>({ on: true, dismissed: [] });
  useEffect(() => { setS(read()); }, []);
  const update = (v: { on: boolean; dismissed: string[] }) => { setS(v); write(v); };
  return <HintsContext.Provider value={{
    on: s.on, dismissed: s.dismissed,
    setOn: on => update({ ...s, on }),
    dismiss: k => update({ ...s, dismissed: [...new Set([...s.dismissed, k])] }),
    reset: () => update({ on: true, dismissed: [] }),
  }}>{children}</HintsContext.Provider>;
}
export const useHints = () => useContext(HintsContext);

/* ------------------------------------------------------------------ glossary */

export const GLOSSARY: Record<string, string> = {
  "sharpe": "Sharpe ratio: return divided by how bumpy the ride was. Above 1 is good, above 2 is great, negative means losing money.",
  "sr": "Sharpe ratio: return divided by how bumpy the ride was. Above 1 is good, above 2 is great, negative means losing money.",
  "max drawdown": "The worst drop from a high point. 10% means at one moment the account was 10% below its best value.",
  "drawdown": "The worst drop from a high point. 10% means at one moment the account was 10% below its best value.",
  "training": "The oldest 60% of history. Each agent's strategy settings were tuned on this period only.",
  "validation": "The next 20% of history, never seen during tuning. Strategies are judged and funded based on this period, which is how overfitting gets caught.",
  "held-out": "The final 20% of history, kept sealed like a final exam. It is run exactly once, after all decisions are frozen, and reported even if it loses.",
  "held-out test": "The final 20% of history, kept sealed like a final exam. It is run exactly once, after all decisions are frozen, and reported even if it loses.",
  "allocation": "How much of the $10,000 of pretend money a strategy was given. Decided by a fixed formula: at most 40% each, 10% always stays in cash.",
  "allocated": "How much of the $10,000 of pretend money a strategy was given. Decided by a fixed formula: at most 40% each, 10% always stays in cash.",
  "equity": "Cash plus the current value of everything owned. The account's total worth right now.",
  "available cash": "Cash that is not set aside for orders still waiting to fill. This is what agents can still spend.",
  "reserved cash": "Cash set aside for buy orders that haven't filled yet, so two agents can never spend the same dollar.",
  "market value": "What all current holdings are worth at the latest prices.",
  "realized p&l": "Profit or loss that is locked in because shares were actually sold.",
  "fees / slippage": "Trading costs. Fees are a per-share charge; slippage is paying a little more (or receiving a little less) than the quoted price.",
  "slippage": "Paying a little more (or receiving a little less) than the quoted price. Real trading always has some; we simulate it.",
  "fills": "Trades that actually happened. An order can fill completely, partly, or not at all.",
  "held-out fills": "How many trades happened during the sealed final-exam period.",
  "held-out net return": "Profit or loss over the sealed final-exam period, which was evaluated only once.",
  "held-out sharpe": "Sharpe ratio over the sealed final-exam period. Above 1 is good, negative means it lost money.",
  "held-out max drawdown": "The worst drop from a high point during the sealed final-exam period.",
  "net return": "Total profit or loss as a percentage of the starting money.",
  "training return": "Profit or loss on the training period, the data the strategy was tuned on. Easy to look good here.",
  "validation return": "Profit or loss on data the strategy never saw while being tuned. This is the number that matters.",
  "gates": "Pass/fail checks on validation: at least 10 trades and a drawdown no worse than 25%. Fail one and the strategy gets $0.",
  "score": "Validation Sharpe × (1 − drawdown) × stability. Higher means stronger and steadier. Capital is split in proportion to it.",
  "stability": "Fraction of three separate slices of history where the strategy made money. 1.00 means it worked in all of them.",
  "kill switch": "Emergency stop. Blocks all new buying. Trips automatically if the account falls 15% from its peak, or can be flipped by hand.",
  "digest": "A short fingerprint (SHA-256) of the whole account state. If anything changes, even one cent, the fingerprint changes.",
  "hash chain": "Each ledger record includes the fingerprint of the one before it, so editing any old record breaks every fingerprint after it.",
  "paper trading": "Simulated trading with pretend money. No real broker, no real orders, no real funds.",
  "momentum": "Bets that prices moving up keep moving up (trend-following).",
  "mean reversion": "Bets that prices that moved too far will snap back toward normal.",
  "volatility": "Owns less when the market is wild and more when it is calm.",
  "breakout": "Buys when a price jumps above its recent high, sells if it falls back below its recent low.",
  "rsi": "Buys after a stretch of heavy selling (low RSI) and sells once the price has bounced back.",
  "vs core": "Out-of-sample return minus a passive equal-weight hold of every symbol. Positive means the strategy beat just buying and holding on data it was not tuned on.",
  "passive core": "Equal-weight buy-and-hold of every symbol. Capital no strategy has earned stays here.",
  "trend": "Holds a stock only while it is up over the last few weeks, and steps aside when it isn't.",
  "frozen": "This research is finished and locked: strategies, settings and allocations can't change anymore.",
  "live": "A paper-trading session is streaming right now: prices arrive and agents trade through the risk engine.",
};

export function Term({ children, k, def }: { children: ReactNode; k?: string; def?: string }) {
  const { on } = useHints();
  const text = def || GLOSSARY[(k || (typeof children === "string" ? children : "")).toLowerCase()];
  if (!text || !on) return <>{children}</>;
  return <span className="term" tabIndex={0}>{children}<span className="term-tip" role="tooltip">{text}</span></span>;
}

/* ------------------------------------------------------------------ risk rules in plain English */

export const RULES: Record<string, string> = {
  R0_PORTFOLIO_NOT_OPEN: "Account not opened yet",
  R1_MAX_ORDER_VALUE: "Order too big (max 25% of account)",
  R2_AGENT_ALLOCATION: "Over the agent's budget",
  R2_ALLOCATIONS_EXCEED_EQUITY: "Budgets exceed the account",
  R3_GROSS_EXPOSURE: "Too much invested (max 90%)",
  R4_BUYING_POWER: "Not enough free cash",
  R5_POSITION: "Selling more than it owns",
  R5_NO_POSITION: "Can't sell what it doesn't own",
  R6_INVALID_QTY: "Invalid quantity",
  R6_INVALID_LIMIT: "Invalid limit price",
  R6_LIMIT_OUTSIDE_BAND: "Limit price too far from market",
  R6_UNKNOWN_SYMBOL: "Unknown stock",
  R7_STALE_SIGNAL: "Decision used old prices",
  R7_FUTURE_SIGNAL: "Decision used future prices",
  R8_DUPLICATE_ID_DIFFERENT_PAYLOAD: "Order ID reused with new details",
  R9_KILL_SWITCH: "Emergency stop is on",
};
export const ruleText = (code: string) => RULES[code] || code.replaceAll("_", " ").toLowerCase();

export function RuleLabel({ code, className = "" }: { code: string; className?: string }) {
  return <span className={`rule-label ${className}`} title={code}>{ruleText(code)}</span>;
}

/* ------------------------------------------------------------------ page intros */

type Intro = { icon: ReactNode; line: string; more?: string[] };
const fmt = (ts?: number) => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "?";

function intros(manifest: Manifest | null): Record<View | "ask", Intro> {
  const sp = manifest?.dataset.splits;
  const dates = sp ? `Tuned on ${fmt(sp.train?.[0])} – ${fmt(sp.train?.[1])} · judged on ${fmt(sp.validation?.[0])} – ${fmt(sp.validation?.[1])} · final exam ${fmt(sp.test?.[0])} – ${fmt(sp.test?.[1])}.` : "History is split into three periods: tune, judge, final exam.";
  return {
    marketplace: { icon: <Store size={18} />, line: "Compare strategies and buy or resell their demo licenses with virtual credits. This wallet is separate from your paper-trading account.",
      more: ["Compare returns, Sharpe and maximum drawdown on the same dataset and evaluation period.", "My licenses shows what you own. Listing a license reserves it until it sells or you cancel.", "Nike Review Radar studies dated customer reviews; it has no measured trading returns yet."] },
    browse: { icon: <Store size={18} />, line: `${manifest?.proposals.length || "Several"} AI agents each proposed a trading strategy. We tested them on data they never saw, then gave the winners pretend money.`,
      more: ["Each card is one agent's strategy. Green pills are returns; the validation one is what counts.", "The big chart compares the sealed final-exam result with the live paper session.", "Everything is simulated with $10,000 of pretend money."] },
    portfolio: { icon: <Briefcase size={18} />, line: "Your simulated account: $10,000 of pretend money, what each AI agent bought, and how it's doing right now.",
      more: ["Equity = cash + the value of everything owned.", "Reserved cash is set aside for orders still waiting, so agents can never double-spend.", "The kill switch here is an emergency stop that blocks new buying."] },
    orders: { icon: <ListOrdered size={18} />, line: "Every trade an agent tried to make, and what the risk engine decided: approve, shrink, or reject.",
      more: ["APPROVE: allowed as asked. REDUCE: allowed, but made smaller to fit a limit. REJECT: blocked.", "The grey line under each decision says which rule decided it.", "You can place your own test order with the ticket above; it goes through the same rules."] },
    research: { icon: <FlaskConical size={18} />, line: "How the strategies were made: what each agent proposed, how it was tuned on old data, and how it scored on data it never saw.",
      more: [dates, "Agents only saw statistics from the tuning period. They pick from 4 approved strategy types and can't write code.", "A strategy that looks great on tuning data but fails the judging period gets $0. That's how overfitting is caught."] },
    audit: { icon: <ShieldCheck size={18} />, line: "Check recorded money movements: every event is fingerprinted and chained, so the books can be rebuilt and checked.",
      more: ["\"Verify full ledger\" re-plays every recorded event from scratch and compares the result with the stored account.", "If anyone edited a single record, the fingerprints would stop matching."] },
    analytics: { icon: <BarChart3 size={18} />, line: "Performance over time: profit per agent, how much money is invested, and how it compares to just buying and holding.",
      more: ["Exposure is how much of the account is invested in stocks rather than sitting in cash.", "The benchmark shows what simply buying every stock equally and holding would have done."] },
    coordinator: { icon: <Play size={18} />, line: "One coordinator, one execution account: every agent's request for a stock becomes at most one order, and this page shows exactly why.",
      more: ["Agents send intents (how much they want to hold), never orders.", "Opposite requests are netted between agents without trading; only the net difference goes to the paper order book.", "Fills here are simulated against a synthetic liquidity ladder built from price bars. They are not exchange fills."] },
    simulation: { icon: <Play size={18} />, line: "Watch a whole simulation play out: the market replays from start to finish while each agent trades, and you see what it was thinking and how it ended.",
      more: ["Each agent's trading rule is fixed code. The AI chose the rule type and wrote the thesis during research, and is asked again only if a scripted crash hits.", "Every order shows what the agent wanted and what the risk engine decided: approve, shrink, or block.", "Results appear when the replay ends, including a comparison with simply buying and holding."] },
    run: { icon: <Play size={18} />, line: "Start your own experiment: pick a dataset, let the AI agents research it, then replay the market with them trading.",
      more: ["Step 1: Launch research. Three agents propose strategies; the best get funded (about a minute).", "Step 2: Launch a session from that research. The final-exam period replays like a live market, optionally with a crash.", "Each session uses a fresh portfolio, so nothing you did before is overwritten."] },
    ask: { icon: <MessageCircle size={18} />, line: "Ask anything about what's happening, by typing or voice. Answers come from the real records, not guesses.",
      more: ["Try: \"Why was the last order rejected?\" or \"Which agent performed best?\"", "This can only read; it can't place or change trades."] },
  };
}

export function PageIntro({ view, manifest }: { view: View | "ask"; manifest: Manifest | null }) {
  const { on, dismissed, dismiss } = useHints();
  const [open, setOpen] = useState(false);
  if (!on || dismissed.includes(view)) return null;
  const it = intros(manifest)[view];
  return <div className="intro" role="note">
    <span className="intro-icon">{it.icon}</span>
    <div className="intro-body">
      <p className="intro-line">{it.line}</p>
      {open && it.more && <ul className="intro-more">{it.more.map(m => <li key={m}>{m}</li>)}</ul>}
    </div>
    {it.more && <button className="intro-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Less" : "Learn more"}<ChevronDown size={14} className={open ? "flip" : ""} /></button>}
    <button className="intro-close" aria-label="Hide this tip" title="Hide this tip" onClick={() => dismiss(view)}><X size={15} /></button>
  </div>;
}

/* ------------------------------------------------------------------ how it works */

export function HowItWorks({ go }: { go: (v: View) => void }) {
  const { on, dismissed, dismiss } = useHints();
  if (!on || dismissed.includes("how")) return null;
  const steps = [
    { n: 1, icon: <FlaskConical size={18} />, t: "Research", d: "A team of AI agents study the market; each proposes a strategy", v: "research" as View },
    { n: 2, icon: <Scale size={18} />, t: "Test", d: "Judged on data they never saw. Overfitters get $0", v: "research" as View },
    { n: 3, icon: <Radio size={18} />, t: "Trade", d: "Market replays live. 10 risk rules check every order", v: "orders" as View },
    { n: 4, icon: <ShieldCheck size={18} />, t: "Verify", d: "Books are replayed and checked. Results sealed on Solana", v: "audit" as View },
  ];
  return <div className="how card">
    <div className="how-head"><span className="eyebrow"><BookOpen size={13} /> How HellaBandz works</span>
      <button className="intro-close" aria-label="Hide how it works" title="Hide" onClick={() => dismiss("how")}><X size={15} /></button></div>
    <div className="how-steps">{steps.map(s => <button key={s.n} className="how-step" onClick={() => go(s.v)}>
      <span className="how-n">{s.n}</span><span className="how-icon">{s.icon}</span>
      <span><b>{s.t}</b><small>{s.d}</small></span></button>)}</div>
  </div>;
}

/* ------------------------------------------------------------------ header toggle */

export function GuideToggle() {
  const { on, setOn, reset } = useHints();
  // Off -> on also restores any tips that were individually hidden.
  return <button className={`ghost-pill guide ${on ? "on" : ""}`} aria-pressed={on} title={on ? "Hide beginner tips" : "Show beginner tips"}
    onClick={() => on ? setOn(false) : reset()}>
    <BookOpen size={15} />Guide
  </button>;
}
