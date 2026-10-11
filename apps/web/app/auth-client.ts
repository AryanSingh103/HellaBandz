"use client";

export function authFetch(input: string, init: RequestInit = {}) {
  return fetch(input, { ...init, credentials: "include", cache: "no-store" });
}
