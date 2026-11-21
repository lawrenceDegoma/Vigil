// End-to-end clip upload and viewing on the LOCAL stack (scripts/sms-dry-run.sh).
// The real Python uploader PUTs a generated MP4 into Storage; real signed-in users
// then ask clip-url for it. Synthetic test pattern only; everything is cleaned up.

import { assert, assertEquals, assertNotEquals } from "jsr:@std/assert@1.0.13";
import postgres from "npm:postgres@3.4.9";
import { newDeviceToken, sha256Hex } from "../../functions/_shared/auth.ts";

const API = Deno.env.get("E2E_API_URL") ?? "http://127.0.0.1:54321";
const DB_URL = Deno.env.get("E2E_DB_URL") ??
  "postgresql://postgres:postgres@127.0.0.1:54322/postgres";
const ANON = Deno.env.get("E2E_ANON_KEY") ?? "";
const SERVICE = Deno.env.get("E2E_SERVICE_KEY") ?? "";
const SECRET = Deno.env.get("VIGIL_DISPATCH_SECRET") ?? "";
const repo = new URL("../../../", import.meta.url).pathname;

const sql = postgres(DB_URL, { max: 2, onnotice: () => {} });
const H = crypto.randomUUID();
const token = newDeviceToken();
const deviceKey = `e2e-clip-${H.slice(0, 8)}`;
const password = crypto.randomUUID();
const users: Record<string, string> = {};
const jwts: Record<string, string> = {};
let deviceId = "";

async function adminCreateUser(email: string): Promise<string> {
  const res = await fetch(`${API}/auth/v1/admin/users`, {
    method: "POST",
    headers: {
      apikey: SERVICE,
      Authorization: `Bearer ${SERVICE}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ email, password, email_confirm: true }),
  });
  const body = await res.json();
  assert(res.ok, JSON.stringify(body));
  return body.id;
}

async function signIn(email: string): Promise<string> {
  const res = await fetch(`${API}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: ANON, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const body = await res.json();
  assert(res.ok, JSON.stringify(body));
  return body.access_token;
}

async function setup() {
  for (const who of ["admin", "family", "outsider"]) {
    const email = `${who}-${H.slice(0, 8)}@example.test`;
    users[who] = await adminCreateUser(email);
    jwts[who] = await signIn(email);
  }
  await sql`insert into public.households (id, name) values (${H}, 'E2E clips')`;
  await sql`insert into public.household_settings (household_id, timezone, warning_alerts_enabled)
            values (${H}, 'America/Los_Angeles', false)`;
  for (const [who, role] of [["admin", "admin"], ["family", "family"]] as const) {
    await sql`insert into public.memberships (household_id, user_id, role, display_name)
              values (${H}, ${users[who]}, ${role}, ${who})`;
  }
  const [d] =
    await sql`insert into public.devices (household_id, device_key, name, subject_name, token_hash)
    values (${H}, ${deviceKey}, 'Living room', 'Rex', ${await sha256Hex(token)}) returning id`;
  deviceId = d.id;
}

async function teardown() {
  const paths = await sql`select f.storage_path from public.clip_files f
    join public.clips c on c.id = f.clip_id where c.household_id = ${H}`;
  if (paths.length) {
    await fetch(`${API}/storage/v1/object/incident-clips`, {
      method: "DELETE",
      headers: {
        apikey: SERVICE,
        Authorization: `Bearer ${SERVICE}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ prefixes: paths.map((p) => p.storage_path) }),
    }).then((r) => r.body?.cancel());
  }
  await sql`delete from public.households where id = ${H}`;
  for (const id of Object.values(users)) {
    await fetch(`${API}/auth/v1/admin/users/${id}`, {
      method: "DELETE",
      headers: { apikey: SERVICE, Authorization: `Bearer ${SERVICE}` },
    }).then((r) => r.body?.cancel());
  }
  await sql.end();
}

async function ingestIncident(severity: "urgent" | "warning", signal: string): Promise<string> {
  const incident = crypto.randomUUID();
  const now = new Date();
  const res = await fetch(`${API}/functions/v1/device-ingest`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: [{
        kind: "incident_event",
        payload: {
          schema_version: "incident-event.v1",
          event_id: crypto.randomUUID(),
          device_id: deviceKey,
          incident_id: incident,
          event_type: "incident_opened",
          signal_type: signal,
          severity,
          occurred_at: now.toISOString(),
          started_at: new Date(now.getTime() - 10_000).toISOString(),
          confidence: 0.9,
          rule_version: "motion-rules-0.1.0+e2e",
        },
      }],
    }),
  });
  const body = await res.json();
  assertEquals(body.results?.[0]?.status, "accepted", JSON.stringify(body));
  return incident;
}

async function makeMp4(dir: string, seconds: number, kbps: number): Promise<string> {
  const file = `${dir}/clip.mp4`;
  const out = await new Deno.Command("ffmpeg", {
    args: [
      "-loglevel",
      "error",
      "-y",
      "-f",
      "lavfi",
      "-i",
      `testsrc=size=640x360:rate=15,noise=alls=40:allf=t`,
      "-t",
      `${seconds}`,
      "-c:v",
      "libx264",
      "-b:v",
      `${kbps}k`,
      "-minrate",
      `${kbps}k`,
      "-maxrate",
      `${kbps}k`,
      "-bufsize",
      `${kbps / 2}k`,
      "-bf",
      "0",
      "-x264-params",
      "keyint=15:min-keyint=15:scenecut=0",
      "-pix_fmt",
      "yuv420p",
      "-movflags",
      "+faststart",
      file,
    ],
  }).output();
  assert(out.success, new TextDecoder().decode(out.stderr));
  return file;
}

async function upload(clipId: string, incident: string, file: string, dir: string, maxMb = 45) {
  const out = await new Deno.Command("uv", {
    args: [
      "run",
      "--project",
      "services/edge",
      "python",
      "supabase/tests/e2e/upload_clip.py",
      `${API}/functions/v1`,
      token,
      clipId,
      incident,
      file,
      dir,
      `${maxMb}`,
    ],
    cwd: repo,
    env: { VIGIL_LOG_LEVEL: "WARNING" },
  }).output();
  assert(out.success, new TextDecoder().decode(out.stderr).slice(-2000));
  return new TextDecoder().decode(out.stdout).trim().split("\n").at(-1)!;
}

function clipUrl(who: string | null, clipId: string, download = false) {
  return fetch(`${API}/functions/v1/clip-url`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: ANON,
      ...(who ? { Authorization: `Bearer ${jwts[who]}` } : {}),
    },
    body: JSON.stringify({ clip_id: clipId, download }),
  });
}

async function sha256OfBytes(b: Uint8Array<ArrayBuffer>) {
  const d = await crypto.subtle.digest("SHA-256", b);
  return Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, "0")).join("");
}

Deno.test({
  name: "clips: alerted-only upload, member viewing, admin-only download, retention",
  sanitizeResources: false,
  sanitizeOps: false,
  async fn(t) {
    assert(ANON && SERVICE && SECRET.length >= 16, "run via scripts/sms-dry-run.sh");
    await setup();
    const dir = await Deno.makeTempDir({ prefix: "vigil-clip-e2e-" });
    try {
      const urgent = await ingestIncident("urgent", "convulsion");
      const clipId = crypto.randomUUID();
      let file = "";

      await t.step(
        "the edge uploader puts an alerted incident's clip in private storage",
        async () => {
          file = await makeMp4(dir, 4, 800);
          assertEquals(await upload(clipId, urgent, file, dir), "uploaded uploaded");
          const [c] =
            await sql`select uploaded_at, expires_at, part_count, device_id from public.clips where id = ${clipId}`;
          assert(c.uploaded_at && c.expires_at);
          assertEquals(c.part_count, 1);
          assertEquals(c.device_id, deviceId);
        },
      );

      await t.step("a family member can view it through a short-lived signed URL", async () => {
        const res = await clipUrl("family", clipId);
        assertEquals(res.status, 200);
        const body = await res.json();
        assertEquals(body.expires_in, 300);
        const video = await fetch(body.parts[0].url);
        assertEquals(video.status, 200);
        const bytes = new Uint8Array(await video.arrayBuffer());
        assertEquals(
          await sha256OfBytes(bytes),
          await sha256OfBytes(new Uint8Array(await Deno.readFile(file))),
        );
      });

      await t.step(
        "only the admin may download; outsiders and anonymous callers get nothing",
        async () => {
          const fam = await clipUrl("family", clipId, true);
          assertEquals(fam.status, 403);
          await fam.body?.cancel();
          const admin = await clipUrl("admin", clipId, true);
          assertEquals(admin.status, 200);
          const dl = await fetch((await admin.json()).parts[0].url);
          assert((dl.headers.get("content-disposition") ?? "").includes("attachment"));
          await dl.body?.cancel();
          const outsider = await clipUrl("outsider", clipId);
          assertEquals(outsider.status, 404);
          await outsider.body?.cancel();
          const anon = await clipUrl(null, clipId);
          assertEquals(anon.status, 401);
          await anon.body?.cancel();
          const audited = await sql`select action from public.audit_log
          where household_id = ${H} and target_id = ${clipId} order by id`;
          assertEquals(audited.map((a) => a.action), ["clip_viewed", "clip_downloaded"]);
        },
      );

      await t.step("users cannot read the bucket directly, even with a valid sign-in", async () => {
        const [p] = await sql`select storage_path from public.clip_files where clip_id = ${clipId}`;
        const direct = await fetch(
          `${API}/storage/v1/object/authenticated/incident-clips/${p.storage_path}`,
          { headers: { apikey: ANON, Authorization: `Bearer ${jwts.family}` } },
        );
        assertNotEquals(direct.status, 200);
        await direct.body?.cancel();
        const list = await fetch(`${API}/storage/v1/object/list/incident-clips`, {
          method: "POST",
          headers: {
            apikey: ANON,
            Authorization: `Bearer ${jwts.admin}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ prefix: H }),
        });
        const listed = list.ok ? await list.json() : [];
        if (!list.ok) await list.body?.cancel();
        assertEquals(listed.length, 0, "even the admin cannot list objects directly");
      });

      await t.step("a large clip arrives as several playable parts", async () => {
        const bigDir = await Deno.makeTempDir({ dir });
        const big = await makeMp4(bigDir, 12, 1500); // ~2.2 MB, uploaded with a 1 MB limit
        const bigId = crypto.randomUUID();
        assertEquals(await upload(bigId, urgent, big, bigDir, 1), "uploaded uploaded");
        const [c] = await sql`select part_count from public.clips where id = ${bigId}`;
        assert(c.part_count >= 2, `parts: ${c.part_count}`);
        const res = await clipUrl("family", bigId);
        const parts = (await res.json()).parts as { url: string }[];
        assertEquals(parts.length, c.part_count);
        for (const p of parts) {
          const r = await fetch(p.url);
          assertEquals(r.status, 200);
          await r.body?.cancel();
        }
      });

      await t.step("clips of incidents that never alerted stay on the device", async () => {
        const warning = await ingestIncident("warning", "vomiting"); // warnings are off here
        assertEquals(
          await upload(crypto.randomUUID(), warning, file, dir),
          "skipped skipped:not_alerted",
        );
      });

      await t.step("expired clips are deleted from storage and the database", async () => {
        const [p] = await sql`select storage_path from public.clip_files where clip_id = ${clipId}`;
        const before = await (await clipUrl("family", clipId)).json();
        await sql`update public.clips set expires_at = now() - interval '1 minute' where household_id = ${H}`;
        const res = await fetch(`${API}/functions/v1/dispatch`, {
          method: "POST",
          headers: { "x-vigil-dispatch-secret": SECRET },
        });
        assert((await res.json()).clipsExpired >= 2);
        assertEquals((await sql`select 1 from public.clips where id = ${clipId}`).length, 0);
        const gone = await fetch(before.parts[0].url); // previously valid link
        assertNotEquals(gone.status, 200);
        await gone.body?.cancel();
        assert(p.storage_path);
      });
    } finally {
      await Deno.remove(dir, { recursive: true });
      await teardown();
    }
  },
});
