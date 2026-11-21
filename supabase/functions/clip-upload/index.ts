// POST /functions/v1/clip-upload   (device token; verify_jwt = false)
// {"action":"register","clip":{...}} -> signed upload URLs for parts not yet uploaded
// {"action":"complete","clip_id":..,"part_index":..} -> verifies size in storage, marks it
// Only clips of alerted incidents are accepted (ADR-010).

import { bearerToken, sha256Hex } from "../_shared/auth.ts";
import { handleClipUpload } from "../_shared/clips.ts";
import { Db } from "../_shared/db.ts";
import { json, readJson } from "../_shared/http.ts";
import { SupabaseStorage } from "../_shared/storage.ts";

const db = Db.fromEnv();
const storage = SupabaseStorage.fromEnv();

Deno.serve(async (req) => {
  if (req.method !== "POST") return json(405, { error: "method not allowed" });
  const token = bearerToken(req);
  const device = token ? await db.authenticateDevice(await sha256Hex(token)) : null;
  if (!device) return json(401, { error: "unauthorized" });
  let body: unknown;
  try {
    body = await readJson(req, 64 * 1024);
  } catch {
    return json(400, { error: "invalid body" });
  }
  try {
    const r = await handleClipUpload(db, storage, device.id, body, new Date());
    return json(r.status, r.body);
  } catch (e) {
    console.error(`clip-upload failed for device ${device.id}: ${(e as Error).message}`);
    return json(500, { error: "internal error" });
  }
});
