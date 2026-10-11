"use client";
import { useEffect, useState, type ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { authConfigured, localAuthDisabled, supabase } from "./auth-client";
import { errorText, initialRoute, request } from "./model";
import { KalshiAccount } from "./kalshi-account";
import styles from "./account.module.css";

type Account = { id: string; email: string; default_portfolio_id: string };

export function AccountGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loaded, setLoaded] = useState(localAuthDisabled || !authConfigured);
  const [account, setAccount] = useState<Account | null>(null);
  const [accountView, setAccountView] = useState(false), [recovery, setRecovery] = useState(false);
  const [error, setError] = useState(""), [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!authConfigured || localAuthDisabled) return;
    let active = true;
    void supabase().auth.getSession().then(({ data, error }) => {
      if (!active) return;
      setSession(data.session); setLoaded(true);
      if (error) setError(error.message);
    });
    const { data: { subscription } } = supabase().auth.onAuthStateChange((event, next) => {
      if (!active) return;
      setSession(next); setLoaded(true);
      if (!next || event === "SIGNED_OUT") { setAccount(null); setAccountView(false); setRecovery(false); }
      if (event === "PASSWORD_RECOVERY") setRecovery(true);
    });
    return () => { active = false; subscription.unsubscribe(); };
  }, []);
  useEffect(() => {
    if (!session) { setAccount(null); return; }
    let active = true;
    setAccount(null); setError("");
    void request<Account>("/v1/account").then(next => {
      if (!active) return;
      initialRoute.p = next.default_portfolio_id;
      const params = new URLSearchParams(location.search);
      // Account switches must not retain another user's resource IDs in the URL.
      const previous = sessionStorage.getItem("qs-account");
      if (!params.get("p") || params.get("p") === "main" || (previous && previous !== next.id)) {
        params.set("p", next.default_portfolio_id); params.delete("run"); params.delete("order");
        history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
      }
      sessionStorage.setItem("qs-account", next.id);
      setAccount(next);
    }).catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [session?.user.id, revision]);
  async function signOut() {
    setError("");
    const { error } = await supabase().auth.signOut({ scope: "local" });
    if (error) setError(error.message);
  }
  if (localAuthDisabled) return <><div className={styles.bar}>Local development: account authentication is explicitly disabled.</div>{children}</>;
  if (!authConfigured) return <div className={styles.screen}><div className={styles.card}><div className={styles.brand}>QuantSwarm</div><h1>Accounts are being connected</h1><p style={{ marginTop: 16 }}>Set the Supabase project URL and publishable key to enable private accounts.</p><p className="muted">Setup instructions: docs/ACCOUNTS_AND_KALSHI.md. Trading stays locked until authentication is configured.</p></div></div>;
  if (!loaded) return <div className={styles.screen} role="status">Restoring your session...</div>;
  if (!session) return <AuthForm />;
  if (recovery) return <AuthForm recovery onRecovered={() => setRecovery(false)} />;
  if (!account || account.id !== session.user.id) return <div className={styles.screen}><div className={styles.card}><h1>Preparing your account</h1>{error ? <><p className={`${styles.notice} ${styles.error}`} role="alert">{error}</p><div className={styles.actions}><button onClick={() => setRevision(n => n + 1)}>Retry</button><button onClick={signOut}>Sign out</button></div></> : <p role="status">Opening your private workspace...</p>}</div></div>;
  return <div key={account.id}><div className={styles.bar}><span>{account.email} · Private account</span><div className={styles.actions}><button onClick={() => setAccountView(false)} aria-pressed={!accountView}>Research workspace</button><button onClick={() => setAccountView(true)} aria-pressed={accountView}>Account & Kalshi</button><button onClick={signOut}>Sign out</button></div></div>{error && <p className={`${styles.notice} ${styles.error}`} role="alert">{error}</p>}{accountView ? <KalshiAccount account={account} /> : children}</div>;
}

function AuthForm({ recovery = false, onRecovered }: { recovery?: boolean; onRecovered?: () => void }) {
  const [mode, setMode] = useState<"signin" | "signup" | "reset">("signin");
  const [email, setEmail] = useState(""), [password, setPassword] = useState("");
  const [pending, setPending] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  async function submit() {
    setPending(true); setError(""); setMessage("");
    try {
      const auth = supabase().auth;
      if (recovery) {
        const { error } = await auth.updateUser({ password }); if (error) throw error;
        setPassword(""); onRecovered?.();
      } else if (mode === "reset") {
        const { error } = await auth.resetPasswordForEmail(email.trim(), { redirectTo: location.origin }); if (error) throw error;
        setMessage("If this email has an account, a password reset link is on its way.");
      } else if (mode === "signup") {
        const { data, error } = await auth.signUp({ email: email.trim(), password, options: { emailRedirectTo: location.origin } }); if (error) throw error;
        if (!data.session) setMessage("Check your email to confirm your account, then sign in.");
      } else {
        const { error } = await auth.signInWithPassword({ email: email.trim(), password }); if (error) throw error;
      }
    } catch (e) { setError(errorText(e)); }
    finally { setPending(false); }
  }
  return <div className={styles.screen}><section className={styles.card}><div className={styles.brand}>QuantSwarm</div><h1>{recovery ? "Choose a new password" : mode === "signup" ? "Create your account" : mode === "reset" ? "Reset your password" : "Welcome back"}</h1><p className="muted" style={{ marginTop: 12 }}>Your research, sessions, and exchange connections belong to your account.</p><form className={styles.form} onSubmit={e => { e.preventDefault(); void submit(); }}>
    {!recovery && <label>Email<input type="email" autoComplete="email" required value={email} onChange={e => setEmail(e.target.value)} disabled={pending} /></label>}
    {(mode !== "reset" || recovery) && <label>Password<input type="password" autoComplete={mode === "signup" || recovery ? "new-password" : "current-password"} minLength={mode === "signup" || recovery ? 8 : undefined} required value={password} onChange={e => setPassword(e.target.value)} disabled={pending} /></label>}
    <button className="primary" disabled={pending}>{pending ? "Please wait..." : recovery ? "Update password" : mode === "signup" ? "Create account" : mode === "reset" ? "Send reset link" : "Sign in"}</button>
  </form>{error && <p className={`${styles.notice} ${styles.error}`} role="alert">{error}</p>}{message && <p className={styles.notice} role="status">{message}</p>}{!recovery && <div className={styles.actions} style={{ marginTop: 18 }}><button className="link" disabled={pending} onClick={() => { setMode(mode === "signin" ? "signup" : "signin"); setError(""); setMessage(""); }}>{mode === "signin" ? "Create an account" : "Back to sign in"}</button>{mode === "signin" && <button className="link" disabled={pending} onClick={() => { setMode("reset"); setError(""); }}>Forgot password?</button>}</div>}<p className="muted" style={{ marginTop: 24, marginBottom: 0, fontSize: 12 }}>Research uses virtual capital. Kalshi demo and production connections are separate.</p></section></div>;
}
