// POST /functions/v1/clip-url   (signed-in user; Supabase JWT)
// {"clip_id": "...", "download": false} -> short-lived signed URLs for each part.
// Members of the clip's household may view; only admins may request download links.
// Every request is audited. Viewing cannot prevent screen recording (DESIGN §7).

import { handleClipUrl } from "../_shared/clips.ts";
import { Db } from "../_shared/db.ts";
import { readJson } from "../_shared/http.ts";
import { SupabaseStorage } from "../_shared/storage.ts";
import { corsHeaders, userIdFromRequest } from "../_shared/user.ts";

const db = Db.fromEnv();
const storage = SupabaseStorage.fromEnv();
const publicUrl = (Deno.env.get("VIGIL_PUBLIC_SUPABASE_URL") ?? Deno.env.get("SUPABASE_URL") ?? "")
  .replace(/\/+$/, "");

function reply(req: Request, status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...corsHeaders(req),
    },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders(req) });
  }
  if (req.method !== "POST") return reply(req, 405, { error: "method not allowed" });
  const userId = await userIdFromRequest(req);
  if (!userId) return reply(req, 401, { error: "unauthorized" });
  let body: unknown;
  try {
    body = await readJson(req, 4096);
  } catch {
    return reply(req, 400, { error: "invalid body" });
  }
  try {
    const r = await handleClipUrl(db, storage, userId, body);
    if (r.status === 200) {
      const parts = (r.body.parts as { index: number; url: string }[])
        .map((p) => ({ index: p.index, url: `${publicUrl}${p.url}` }));
      return reply(req, 200, { ...r.body, parts });
    }
    return reply(req, r.status, r.body);
  } catch (e) {
    console.error(`clip-url failed: ${(e as Error).message}`);
    return reply(req, 500, { error: "internal error" });
  }
});
