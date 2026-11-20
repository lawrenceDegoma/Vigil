// POST /functions/v1/device-ingest
// Authorization: Bearer <device token>   (not a Supabase JWT; verify_jwt = false)
// Body: {"messages": [{"kind": "incident_event" | "heartbeat", "payload": {...}}]}
// Alerts queued by this request are dispatched before replying, so first texts
// do not wait for the next cron tick.

import { bearerToken, sha256Hex } from "../_shared/auth.ts";
import { Db } from "../_shared/db.ts";
import { dispatch } from "../_shared/dispatcher.ts";
import { json, readJson } from "../_shared/http.ts";
import { BadRequest, ingestBatch, MAX_BODY_BYTES } from "../_shared/ingest.ts";
import { providerFromEnv } from "../_shared/sms.ts";

const db = Db.fromEnv();
const provider = providerFromEnv((k) => Deno.env.get(k));

Deno.serve(async (req) => {
  if (req.method !== "POST") return json(405, { error: "method not allowed" });
  const token = bearerToken(req);
  const device = token ? await db.authenticateDevice(await sha256Hex(token)) : null;
  if (!device) return json(401, { error: "unauthorized" });

  let body: unknown;
  try {
    body = await readJson(req, MAX_BODY_BYTES);
  } catch (e) {
    return json(e instanceof RangeError ? 413 : 400, { error: "invalid body" });
  }

  try {
    const report = await ingestBatch(db, device, body, new Date());
    if (report.notificationsQueued > 0) {
      try {
        await dispatch(db, provider, {
          periodic: false,
          deadlineMs: 8_000,
          appUrl: Deno.env.get("VIGIL_APP_URL"),
        });
      } catch (e) {
        // Messages stay queued; the cron dispatcher retries them.
        console.error(`inline dispatch failed: ${(e as Error).message}`);
      }
    }
    return json(200, { results: report.results });
  } catch (e) {
    if (e instanceof BadRequest) return json(400, { error: e.message });
    console.error(`ingest failed for device ${device.id}: ${(e as Error).message}`);
    return json(500, { error: "internal error" });
  }
});
