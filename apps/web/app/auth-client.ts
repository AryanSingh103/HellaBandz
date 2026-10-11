"use client";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const projectUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const publicKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
export const localAuthDisabled = process.env.NEXT_PUBLIC_QS_AUTH_DISABLED === "true";
export const authConfigured = /^https:\/\//.test(projectUrl) && !!publicKey;
let client: SupabaseClient | null = null;
export function supabase() {
  if (!authConfigured) throw new Error("Supabase account authentication is not configured.");
  if (!client) client = createClient(projectUrl, publicKey, { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } });
  return client;
}
export async function accessToken(): Promise<string> {
  if (localAuthDisabled) return "";
  const { data, error } = await supabase().auth.getSession();
  if (error || !data.session) throw new Error("Sign in to access your account.");
  return data.session.access_token;
}
export async function authFetch(input: string, init: RequestInit = {}) {
  const token = await accessToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers, cache: "no-store" });
}
