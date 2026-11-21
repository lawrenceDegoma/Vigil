// End-to-end SMS dry run against the LOCAL stack (scripts/sms-dry-run.sh).
// A device posts real edge-format messages over HTTP; the Edge Functions ingest
// them and send through the mock provider; we check what each phone would get.
// Fake +1555 numbers only; everything created here is deleted afterwards.

import { assert, assertEquals, assertMatch } from "jsr:@std/assert@1.0.13";
import postgres from "npm:postgres@3.4.9";
import { newDeviceToken, sha256Hex } from "../../functions/_shared/auth.ts";

const DB_URL = Deno.env.get("E2E_DB_URL") ??
  "postgresql://postgres:postgres@127.0.0.1:54322/postgres";
const FN_URL = Deno.env.get("E2E_FUNCTIONS_URL") ?? "http://127.0.0.1:54321/functions/v1";
const SECRET = Deno.env.get("VIGIL_DISPATCH_SECRET") ?? "";

const sql = postgres(DB_URL, { max: 2, onnotice: () => {} });
const examples = new URL("../../../packages/contracts/examples/", import.meta.url);
const load = async (f: string) => JSON.parse(await Deno.readTextFile(new URL(f, examples)));

const H = crypto.randomUUID();
const users = { admin: crypto.randomUUID(), fam1: crypto.randomUUID(), fam2: crypto.randomUUID() };
const phones = { admin: "+15555550901", fam1: "+15555550902", fam2: "+15555550903" };
const token = newDeviceToken();
const deviceKey = `e2e-${H.slice(0, 8)}`;
let deviceId = "";

async function setup() {
  await sql`insert into auth.users (id, email) values
    (${users.admin}, ${`admin-${H}@example.test`}), (${users.fam1}, ${`f1-${H}@example.test`}),
    (${users.fam2}, ${`f2-${H}@example.test`})`;
  await sql`insert into public.households (id, name) values (${H}, 'E2E household')`;
  await sql`insert into public.household_settings (household_id, timezone) values (${H}, 'America/Los_Angeles')`;
  for (const [who, role] of [["admin", "admin"], ["fam1", "family"], ["fam2", "family"]] as const) {
    const [m] = await sql`insert into public.memberships (household_id, user_id, role, display_name)
      values (${H}, ${users[who]}, ${role}, ${who}) returning id`;
    await sql`insert into public.notification_contacts (membership_id, phone_e164) values (${m.id}, ${
      phones[who]
    })`;
  }
  const [d] =
    await sql`insert into public.devices (household_id, device_key, name, subject_name, token_hash)
    values (${H}, ${deviceKey}, 'Living room', 'Rex', ${await sha256Hex(token)}) returning id`;
  deviceId = d.id;
}

async function teardown() {
  await sql`delete from public.households where id = ${H}`;
  await sql`delete from auth.users where id in ${sql(Object.values(users))}`;
  await sql.end();
}

function post(path: string, body: unknown, headers: Record<string, string>) {
  return fetch(`${FN_URL}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

const ingest = (messages: unknown[], tok = token) =>
  post("device-ingest", { messages }, { Authorization: `Bearer ${tok}` });
const runDispatch = () => post("dispatch", {}, { "x-vigil-dispatch-secret": SECRET });

async function sent(kind: string, incident?: string) {
  return await sql`select to_e164, body, provider, status from public.notifications
    where household_id = ${H} and kind = ${kind}
      and (${incident ?? null}::uuid is null or incident_id = ${incident ?? null}::uuid)
    order by to_e164`;
}

function freshEvent(base: Record<string, unknown>, incidentId: string, type: string) {
  const now = new Date();
  return {
    ...base,
    event_id: crypto.randomUUID(),
    incident_id: incidentId,
    device_id: deviceKey,
    event_type: type,
    occurred_at: now.toISOString(),
    started_at: new Date(now.getTime() - 10_000).toISOString(),
  };
}

Deno.test({
  name: "SMS dry run: alert fan-out, idempotency, reminders, acknowledgement, offline",
  sanitizeResources: false,
  sanitizeOps: false,
  async fn(t) {
    assert(SECRET.length >= 16, "VIGIL_DISPATCH_SECRET must be set (scripts/sms-dry-run.sh)");
    await setup();
    try {
      const opened = await load("incident_opened.json");
      // The captured example is the edge's start-up heartbeat (camera not yet open,
      // correctly "degraded"); report a healthy device here.
      const example = await load("heartbeat.json");
      const heartbeat = {
        ...example,
        device_id: deviceKey,
        camera: { ...example.camera, ok: true },
        detector: { ...example.detector, ok: true },
      };
      const incident = crypto.randomUUID();
      const openedEvent = freshEvent(opened, incident, "incident_opened");

      await t.step("unauthenticated and malformed requests are refused", async () => {
        assertEquals(
          (await ingest([{ kind: "heartbeat", payload: heartbeat }], newDeviceToken())).status,
          401,
        );
        const noAuth = await post("device-ingest", { messages: [] }, {});
        assertEquals(noAuth.status, 401);
        await noAuth.body?.cancel();
        assertEquals(await runDispatch().then((r) => r.status), 200);
        const badSecret = await post("dispatch", {}, {
          "x-vigil-dispatch-secret": "nope-nope-nope-nope",
        });
        assertEquals(badSecret.status, 401);
        await badSecret.body?.cancel();
      });

      await t.step("urgent incident texts every family phone immediately", async () => {
        const res = await ingest([
          { kind: "heartbeat", payload: heartbeat },
          { kind: "incident_event", payload: openedEvent },
          {
            kind: "incident_event",
            payload: { ...openedEvent, event_id: crypto.randomUUID(), device_id: "someone-else" },
          },
        ]);
        assertEquals(res.status, 200);
        const body = await res.json();
        assertEquals(body.results.map((r: { status: string }) => r.status), [
          "accepted",
          "accepted",
          "rejected",
        ]);
        const alerts = await sent("incident_alert", incident);
        assertEquals(alerts.map((a) => a.to_e164), [phones.admin, phones.fam1, phones.fam2]);
        for (const a of alerts) {
          assertEquals(a.status, "sent");
          assertEquals(a.provider, "mock");
          assertMatch(
            a.body,
            /^Vigil URGENT: possible seizure-like movement seen for Rex at \d{1,2}:\d{2} [AP]M \(Living room\)\. Please check now\./,
          );
        }
        const [dev] = await sql`select status from public.device_status_v where id = ${deviceId}`;
        assertEquals(dev.status, "online");
      });

      await t.step("replaying the same batch sends nothing new", async () => {
        const res = await ingest([{ kind: "incident_event", payload: openedEvent }]);
        assertEquals((await res.json()).results[0].status, "duplicate");
        assertEquals((await sent("incident_alert", incident)).length, 3);
      });

      await t.step("unacknowledged: reminder round 2 goes to everyone", async () => {
        await sql`update public.incidents set alerted_at = now() - interval '301 seconds' where id = ${incident}`;
        assertEquals((await runDispatch()).status, 200);
        const reminders = await sent("incident_reminder", incident);
        assertEquals(reminders.length, 3);
        assert(
          reminders.every((r) => r.status === "sent" && r.body.startsWith("Vigil reminder 1/2:")),
        );
      });

      await t.step("acknowledgement by one family member stops further reminders", async () => {
        await sql.begin(async (tx) => {
          await tx`select set_config('request.jwt.claims', ${
            JSON.stringify({ sub: users.fam1, role: "authenticated" })
          }, true)`;
          await tx`set local role authenticated`;
          const [r] = await tx`select public.incident_action(${incident}, 'acknowledged') as s`;
          assertEquals(r.s, "acknowledged");
        });
        await sql`update public.incidents set alerted_at = now() - interval '700 seconds' where id = ${incident}`;
        await runDispatch();
        assertEquals(
          (await sent("incident_reminder", incident)).length,
          3,
          "no round 3 after acknowledgement",
        );
      });

      await t.step(
        "stale heartbeat: offline notice to the admin only, recovery on return",
        async () => {
          await sql`update public.devices set last_heartbeat_at = now() - interval '10 minutes' where id = ${deviceId}`;
          await runDispatch();
          const offline = await sent("device_offline");
          assertEquals(offline.map((o) => o.to_e164), [phones.admin]);
          assertMatch(offline[0].body, /^Vigil: Living room is OFFLINE\. No heartbeat since /);
          await ingest([{ kind: "heartbeat", payload: heartbeat }]);
          await runDispatch();
          const back = await sent("device_recovered");
          assertEquals(back.map((o) => o.to_e164), [phones.admin]);
          assertEquals(back[0].status, "sent");
        },
      );

      await t.step(
        "the real Python edge pipeline alerts the family through the cloud",
        async () => {
          // Synthetic convulsion -> detector -> incident FSM -> SQLite outbox -> CloudSync ->
          // device-ingest -> engine -> mock SMS. Same code path as the Pi, minus the camera.
          const dataDir = await Deno.makeTempDir({ prefix: "vigil-e2e-" });
          try {
            const out = await new Deno.Command("uv", {
              args: [
                "run",
                "--project",
                "services/edge",
                "vigil",
                "run",
                "--source",
                "synthetic:convulsion",
                "--fast",
                "--data-dir",
                dataDir,
                "--device-id",
                deviceKey,
                "--set",
                "recording.enabled=false",
                "--set",
                "cloud.enabled=true",
                "--set",
                `cloud.endpoint=${FN_URL}`,
              ],
              cwd: new URL("../../../", import.meta.url).pathname,
              env: { VIGIL_DEVICE_TOKEN: token, VIGIL_LOG_LEVEL: "WARNING" },
              stdout: "piped",
              stderr: "piped",
            }).output();
            assert(out.success, new TextDecoder().decode(out.stderr).slice(-2000));
          } finally {
            await Deno.remove(dataDir, { recursive: true });
          }
          const [edgeIncident] = await sql`select id, signal_type, severity, ended_at
          from public.incidents where device_id = ${deviceId} and id <> ${incident}`;
          assertEquals([edgeIncident.signal_type, edgeIncident.severity], ["convulsion", "urgent"]);
          assert(edgeIncident.ended_at !== null, "incident_ended was delivered too");
          const alerts = await sent("incident_alert", edgeIncident.id);
          assertEquals(alerts.map((a) => a.to_e164), [phones.admin, phones.fam1, phones.fam2]);
          assert(alerts.every((a) => a.status === "sent" && a.body.startsWith("Vigil URGENT:")));
        },
      );

      await t.step(
        "test alert: TEST text to every phone, no incident, capped at 3 per hour",
        async () => {
          const incidentsBefore = await sql`select count(*)::int as n from public.incidents
          where household_id = ${H}`;
          const request = (n: number) => ({
            kind: "test_alert",
            payload: {
              schema_version: "test-alert.v1",
              request_id: crypto.randomUUID(),
              device_id: deviceKey,
              requested_at: new Date().toISOString(),
              scope: n === 0 ? "all" : "admin",
            },
          });
          const first = await (await ingest([request(0)])).json();
          assertEquals(first.results[0].status, "accepted");
          const texts = await sent("test_alert");
          assertEquals(texts.map((x) => x.to_e164), [phones.admin, phones.fam1, phones.fam2]);
          for (const x of texts) {
            assertEquals(x.status, "sent");
            assertMatch(
              x.body,
              /^Vigil TEST from Living room at .*only a test.*Reply STOP to opt out\.$/,
            );
          }
          await ingest([request(1)]);
          await ingest([request(2)]);
          await ingest([request(3)]); // fourth within the hour: ignored
          assertEquals((await sent("test_alert")).length, 3 + 1 + 1);
          const incidentsAfter = await sql`select count(*)::int as n from public.incidents
          where household_id = ${H}`;
          assertEquals(incidentsAfter[0].n, incidentsBefore[0].n);
        },
      );

      await t.step("no full phone numbers in function logs (checked by the script)", () => {});
    } finally {
      await teardown();
    }
  },
});
