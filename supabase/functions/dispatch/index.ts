// POST /functions/v1/dispatch   (called every minute by pg_cron; verify_jwt = false)
// Header x-vigil-dispatch-secret must equal VIGIL_DISPATCH_SECRET.
// Schedules reminders, raises admin-only offline notices, sends due messages.

import { safeEqual } from "../_shared/auth.ts";
import { Db } from "../_shared/db.ts";
import { dispatch } from "../_shared/dispatcher.ts";
import { json } from "../_shared/http.ts";
import { providerFromEnv } from "../_shared/sms.ts";

const db = Db.fromEnv();
const provider = providerFromEnv((k) => Deno.env.get(k));
const secret = Deno.env.get("VIGIL_DISPATCH_SECRET") ?? "";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json(405, { error: "method not allowed" });
  const given = req.headers.get("x-vigil-dispatch-secret") ?? "";
  if (secret.length < 16 || !safeEqual(given, secret)) return json(401, { error: "unauthorized" });
  try {
    const report = await dispatch(db, provider, {
      deadlineMs: 20_000,
      appUrl: Deno.env.get("VIGIL_APP_URL"),
    });
    const pruned = await db.pruneHealth(new Date());
    return json(200, { ...report, pruned });
  } catch (e) {
    console.error(`dispatch failed: ${(e as Error).message}`);
    return json(500, { error: "internal error" });
  }
});
