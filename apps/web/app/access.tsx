"use client";
import { ReactNode, createContext, useContext, useEffect, useState } from "react";
import { API, errorText, request } from "./model";

const AuthRequired = createContext(false);

export function AccessGate({children}:{children:ReactNode}) {
  const [ready,setReady]=useState(false),[loaded,setLoaded]=useState(false),[key,setKey]=useState(""),[error,setError]=useState(""),[pending,setPending]=useState(false),[required,setRequired]=useState(false);
  async function check(){try{const response=await fetch(API+"/v1/auth/status",{credentials:"include",cache:"no-store",signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error("API authentication status is unavailable");const s=await response.json() as {required:boolean;authenticated:boolean};setRequired(s.required);setReady(!s.required||s.authenticated);setError("");}catch(e){setError(errorText(e));}finally{setLoaded(true);}}
  useEffect(()=>{void check();const expired=()=>setReady(false);window.addEventListener("qs-auth-expired",expired);return()=>window.removeEventListener("qs-auth-expired",expired);},[]);
  async function login(){setPending(true);setError("");try{await request("/v1/auth/session",{},{method:"POST",headers:{Authorization:`Bearer ${key}`}});setKey("");setReady(true);}catch(e){setError(errorText(e));}finally{setPending(false);}}
  if(ready)return <AuthRequired.Provider value={required}>{children}</AuthRequired.Provider>;
  return <main style={{maxWidth:520,margin:"12vh auto",padding:24}}><a className="wordmark" href="/">Hella<span>Bandz</span></a><section><h1>{loaded?"Operator sign-in":"Connecting..."}</h1><p>Enter your local access key to open portfolios, research and audit records.</p><form onSubmit={e=>{e.preventDefault();void login();}}><label>Access key<input aria-label="Operator access key" type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)}/></label><button className="primary" disabled={!loaded||pending||!key}>{pending?"Signing in...":"Sign in"}</button></form>{error&&<p role="alert">{error}</p>}<p className="muted">Your access key is kept in memory during sign-in. The browser receives an expiring HttpOnly session.</p><button onClick={check}>Retry connection</button></section></main>;
}

export function SignOut(){if(!useContext(AuthRequired))return null;return <button className="ghost-pill" onClick={async()=>{await request("/v1/auth/session",{},{method:"DELETE"});location.reload();}}>Sign out</button>;}
