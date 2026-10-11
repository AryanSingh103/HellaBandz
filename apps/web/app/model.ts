import { authFetch } from "./auth-client";
export const API = process.env.NEXT_PUBLIC_API || "http://localhost:8000";
export const validId = (s: string) => /^[A-Za-z0-9_.:\-]{1,96}$/.test(s);
export type View = "browse" | "simulation" | "marketplace" |"portfolio" | "orders" | "research" | "audit" | "analytics" | "coordinator" | "run";
export type Route = { p: string; view: View; run: string; category: string; q: string; sort: string; dataset: string; order: string };
export const initialRoute: Route = { p: "main", view: "browse", run: "", category: "All strategies", q: "", sort: "score", dataset: "", order: "" };
export function url(path: string, params: Record<string, string | number | boolean | undefined> = {}) {
  const u = new URL(API + path);
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") u.searchParams.set(k, String(v)); });
  return u.toString();
}
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
export async function request<T>(path: string, params: Record<string, string | number | boolean | undefined> = {}, init: RequestInit = {}): Promise<T> {
  const response = await authFetch(url(path, params), { ...init, signal: init.signal || AbortSignal.timeout(45000) });
  const data = await response.json().catch(() => null);
  if (response.status === 401 && path !== "/v1/auth/session" && typeof window !== "undefined") window.dispatchEvent(new Event("qs-auth-expired"));
  if (!response.ok) throw new ApiError(response.status, typeof data?.detail === "string" ? data.detail :
    Array.isArray(data?.detail) ? data.detail.map((d: { msg: string }) => d.msg).join("; ") : data?.detail ? JSON.stringify(data.detail) : `API request failed (${response.status})`);
  if (data === null) throw new Error("API returned an invalid response");
  return data as T;
}
export const post = (body: unknown, signal?: AbortSignal, key?: string): RequestInit => ({ method: "POST", body: JSON.stringify(body),
  headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) }, signal });
export const errorText = (e: unknown) => e instanceof Error ? e.message : "Request failed. Retry when the API is available.";
export const money = (n?: number, currency = "USD") => n === undefined || !Number.isFinite(n) ? "Unavailable" :
  /^[A-Z]{3}$/.test(currency) ? new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(n / 1e6) :
    `${new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n / 1e6)} ${currency}`;
export const pct = (n?: number) => n === undefined || !Number.isFinite(n) ? "Unavailable" : `${(n * 100).toFixed(2)}%`;
export const id = (prefix = "ui") => `${prefix}-${crypto.randomUUID()}`;
export function moneyInput(value: string, positive = false) {
  if (!/^\d+(\.\d{1,6})?$/.test(value)) return false;
  const n = Number(value) * 1e6;
  return Number.isSafeInteger(Math.round(n)) && (positive ? Math.round(n) > 0 : n >= 0);
}
export const familyName = (s: string) => ({ sma_crossover: "Moving Average Crossover", breakout: "Price Breakout", zscore_reversion: "Z-Score Reversion", vol_target: "Volatility Target", rsi_reversion: "RSI Reversion", ts_momentum: "Trend Momentum", regime_reversion: "Range-Regime Reversion Overlay", buy_hold: "Passive Core" }[s] || s);
export type Band = { start: number; end: number; label: string };
/** Scheduled market-crash window of a browser-launched session, located on its equity points (one point per replay step). */
export function crashBand(jobs: Job[] | undefined, pid: string, equity: { ts: string }[] | undefined, session?: Context["session"]): Band | undefined {
  const job = jobs?.find(j => j.kind === "session" && j.portfolio_id === pid) as (Job & { config?: { steps?: number; crash_at?: number | null } }) | undefined;
  // Command-line sessions have no job row; their summary records the crash step instead.
  const steps = job ? job.config?.steps : session?.steps;
  const at = job ? job.config?.crash_at : session?.steps && session.crash_start_step != null ? session.crash_start_step / session.steps : undefined;
  if (!steps || at === undefined || at === null || !equity?.length) return undefined;
  const pts = [...equity].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts)), k = Math.floor(steps * at);
  if (!pts[k]) return undefined;
  return { start: Date.parse(pts[k].ts) / 1000, end: Date.parse(pts[Math.min(k + 4, pts.length - 1)].ts) / 1000, label: "Simulated crash" };
}
export const stamp = (s?: string | number | null) => s ? new Date(typeof s === "number" ? s * 1000 : s).toLocaleString() : "Unavailable";
export type Metrics = { net_return: number; sharpe: number; max_drawdown: number; n_fills: number; fees_usd: number; slippage_usd: number; final_equity_usd: number };
// `excess` and `core` exist on runs scored against the passive core; older frozen manifests have neither.
export type Ranked = { strategy_id: string; agent_id: string; family: string; params: Record<string, number>; train: Metrics; validation: Metrics; score: number; gates: string[]; stability: number;
  excess?: { validation_excess_return: number; out_of_sample_excess_return: number; out_of_sample_information_ratio: number } };
export type Manifest = { run_id: string; created_at: string; dataset: { dataset_id: string; source: string; content_sha256: string; splits: Record<string, number[]> };
  ranked: Ranked[]; fitted: { spec: { strategy_id: string; agent_id: string; universe: string[]; params: Record<string, number> } }[];
  proposals: { agent_id: string; source: string; proposal: { rationale: string; evidence: string[]; param_grid: Record<string, number[]>; universe: string[] } }[];
  allocations: { by_strategy: Record<string, { weight: number; capital_micros: number }> }; supervisor: { source: string; rationale: string };
  core?: { spec: { strategy_id: string; universe: string[] }; train: Metrics; validation: Metrics };
  held_out_test: { portfolio: { metrics: Metrics }; per_strategy_standalone: Record<string, Metrics>; core_standalone?: Metrics; excess_return_vs_core?: number; equity_curve: { ts: number[]; equity: number[] } }; policy: Record<string, number> };
export type Tournament = { run_id: string; manifest: Manifest; manifest_hash: string; created_at: string; solana_sig?: string | null };
export type Dataset = { dataset_id: string; source: string; content_sha256: string; symbols: string[]; interval_s: number; first_ts: number; last_ts: number; row_count: number;
  splits: Record<string, number[]>; execution_usable: boolean; unavailable_reason?: string; quality: unknown; synthetic: boolean; quote_currency: string; lots_per_base_unit: number; units?: string };
export type Position = { qty: number; basis: number; reserved: number };
export type State = { opened: boolean; initial_cash: number; cash: number; equity: number; market_value: number; reserved_cash: number; realized_pnl: number; fees_paid: number; slippage_cost: number;
  allocations: Record<string, number>; positions: Record<string, Position>; orders: Record<string, unknown>; last_bar: Record<string, [number, number]>; kill_switch: boolean; kill_reason: string };
export type Portfolio = { seq: number; digest: string; head_hash: string; state: State; equity: number; updated_at: string };
export type Runtime = { worker_alive: boolean; worker_last_heartbeat: string | null; pending_commands: number; ready: boolean; ownership: string; manager_alive: boolean;
  intentional_down: boolean; status: string; recovery: unknown; managed?: { error?: string } };
export type Context = { portfolio_id: string; run_id?: string; dataset_id?: string; provenance?: string; manifest: Manifest | null; manifest_hash?: string;
  session?: { steps?: number; crash_start_step?: number | null } };
export type PortfolioChoice = { portfolio_id: string; run_id?: string; ownership: string };
export type Job = { job_id: string; request_id: string; kind: string; run_id: string; portfolio_id: string | null; dataset_id: string; status: string; stage: string;
  progress: number; total: number | null; error: string | null; result: unknown; created_at: string; started_at: string; finished_at: string; stop_requested: boolean };
export type Event = { seq: number; type: string; hash: string; order_id?: string; agent_id?: string; symbol?: string; side?: string; qty?: number; px?: number; fee?: number; rules?: string[] };
export type Order = { order_id: string; agent_id: string; symbol: string; side: string; requested_qty: number; approved_qty: number; filled_qty: number; limit_px?: number; order_type: string; status: string; outcome: string; rules: string[]; cursor_seq: number };
export type HistoryItem = Order & { seq: number; type: string; event: Event; cursor_seq: number; command_seq: number; hash: string; created_at: string };
export type HistoryPage = { items: HistoryItem[]; snapshot_seq: number; next_cursor: number | null };
export type Verification = { status?: string; events: number; chain_ok: boolean | null; match: boolean | null; error?: string; stored_digest?: string; replayed_digest?: string; head_hash?: string };
export type Replay = { event: Event; before: State; after: State; through_seq: number; before_digest: string; after_digest: string; progress: number; total: number };
export type Point = { seq: number; ts: number; equity: number; exposure: number; agents: Record<string, { pnl: number; exposure: number }> };
export type Analytics = { points: Point[]; definitions: Record<string, string>; benchmark: { status: string; reason?: string; assumptions?: string; start_ts?: number; end_ts?: number;
  baseline?: { ts: number; equity: number }[]; strategy?: { ts: number[]; equity: number[] }; baseline_metrics?: Metrics; strategy_metrics?: Metrics } };
export type CoordinatedAgent = { agent_id: string; strategy_id: string; version: string; family: string; params: Record<string, number>; universe: string[];
  funded: boolean; capital: number; status: string; reasons: string[]; hypothesis?: string; proposal_source?: string; evaluation?: Record<string, unknown>; experimental?: boolean; research_approved?: boolean };
export type CoordinatedContext = { current: boolean; policy_version: string; code_version: string; source_sha256: string; registry_sha256: string;
  dataset: Dataset; agents: CoordinatedAgent[]; research: Record<string, unknown>; evaluation_status: string; depth: string; allocation_mode?: "approved" | "experimental"; allocation_note?: string };
export type Sleeve = { capital: number; cash: number; equity: number; market_value: number; pnl: number; fees: number; slippage: number;
  fills: number; positions: Record<string, { qty: number; basis: number }> };
export type CoordinatedReport = { schema: string; portfolio_id: string; context: CoordinatedContext; status: string;
  points: Point[]; steps: { step: number; ts: number; equity: number; cash: number; closes: Record<string, number>; sleeves: Record<string, Sleeve>; unallocated_cash?: number; fees?: number; slippage?: number }[];
  decisions: Record<string, unknown>[]; rule_readings: Record<string, unknown>[]; summary?: Record<string, unknown>;
  final_attribution?: Record<string, Sleeve>; benchmark: Analytics["benchmark"] };

/** Shared ordering for measured strategy performance; missing results sort last. */
export function comparePerformance(a: Metrics | null | undefined, b: Metrics | null | undefined, field: "net_return" | "sharpe" | "max_drawdown") {
  const av = a?.[field], bv = b?.[field];
  if (av == null || !Number.isFinite(av)) return bv == null || !Number.isFinite(bv) ? 0 : 1;
  if (bv == null || !Number.isFinite(bv)) return -1;
  return field === "max_drawdown" ? av - bv : bv - av;
}
export function matchesSearch(query: string, ...fields: string[]) {
  return fields.join(" ").toLowerCase().includes(query.trim().toLowerCase());
}
