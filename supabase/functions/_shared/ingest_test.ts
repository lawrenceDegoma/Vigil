import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.13";
import { BadRequest, ingestBatch, type IngestDb } from "./ingest.ts";
import type { IngestResult } from "./db.ts";

const examples = new URL("../../../packages/contracts/examples/", import.meta.url);
const opened = JSON.parse(await Deno.readTextFile(new URL("incident_opened.json", examples)));
const heartbeat = JSON.parse(await Deno.readTextFile(new URL("heartbeat.json", examples)));
const device = {
  id: "44444444-0000-0000-0000-000000000001",
  household_id: "h",
  device_key: "pi-livingroom",
};

class RecordingDb implements IngestDb {
  calls: string[] = [];
  ingestIncidentEvent(): Promise<IngestResult> {
    this.calls.push("event");
    return Promise.resolve({ status: "accepted", notifications: 3 });
  }
  ingestHeartbeat(): Promise<IngestResult> {
    this.calls.push("heartbeat");
    return Promise.resolve({ status: "accepted", notifications: 0 });
  }
  ingestTestAlert(): Promise<IngestResult> {
    this.calls.push("test");
    return Promise.resolve({ status: "accepted", notifications: 2 });
  }
}

Deno.test("valid messages are applied; invalid ones are rejected without touching the db", async () => {
  const db = new RecordingDb();
  const report = await ingestBatch(db, device, {
    messages: [
      { kind: "incident_event", payload: opened },
      { kind: "heartbeat", payload: heartbeat },
      { kind: "incident_event", payload: { ...opened, confidence: 7 } },
      { kind: "selfie", payload: {} },
    ],
  }, new Date());
  assertEquals(db.calls, ["event", "heartbeat"]);
  assertEquals(report.notificationsQueued, 3);
  assertEquals(report.results.map((r) => r.status), [
    "accepted",
    "accepted",
    "rejected",
    "rejected",
  ]);
  assertEquals(report.results[3].reason, "unknown message kind");
});

Deno.test("batch shape is enforced", async () => {
  const db = new RecordingDb();
  const bodies = [{}, { messages: [] }, { messages: "x" }, { messages: Array(101).fill({}) }, null];
  for (const body of bodies) {
    await assertRejects(() => ingestBatch(db, device, body, new Date()), BadRequest);
  }
  assertEquals(db.calls, []);
});

Deno.test("test alerts are validated and routed", async () => {
  const db = new RecordingDb();
  const ok = {
    schema_version: "test-alert.v1",
    request_id: "d0000000-0000-0000-0000-000000000001",
    device_id: "pi-livingroom",
    requested_at: "2026-11-21T12:00:00Z",
    scope: "all",
  };
  const report = await ingestBatch(db, device, {
    messages: [
      { kind: "test_alert", payload: ok },
      { kind: "test_alert", payload: { ...ok, scope: "everyone" } },
    ],
  }, new Date());
  assertEquals(db.calls, ["test"]);
  assertEquals(report.notificationsQueued, 2);
  assertEquals(report.results.map((r) => r.status), ["accepted", "rejected"]);
});
