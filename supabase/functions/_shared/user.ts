// Resolve the signed-in user from a Supabase access token (asks Auth, so it works
// with any signing key setup). Returns null for missing/invalid/expired tokens.

export async function userIdFromRequest(req: Request, fetchImpl: typeof fetch = fetch) {
  const m = /^Bearer (\S+)$/.exec(req.headers.get("authorization") ?? "");
  if (!m) return null;
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_ANON_KEY") ?? Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!url || !key) throw new Error("SUPABASE_URL / key missing");
  const res = await fetchImpl(`${url}/auth/v1/user`, {
    headers: { Authorization: `Bearer ${m[1]}`, apikey: key },
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    await res.body?.cancel();
    return null;
  }
  const user = await res.json().catch(() => null) as { id?: string } | null;
  return typeof user?.id === "string" ? user.id : null;
}

/** CORS for browser callers: only the configured dashboard origin. */
export function corsHeaders(req: Request): Record<string, string> {
  const allowed = Deno.env.get("VIGIL_APP_URL")?.replace(/\/+$/, "");
  const origin = req.headers.get("origin");
  if (!allowed || origin !== allowed) return {};
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}
