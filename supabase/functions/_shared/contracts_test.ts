import { assertEquals } from "jsr:@std/assert@1.0.13";
import Ajv2020 from "npm:ajv@8.20.0/dist/2020.js";
import addFormats from "npm:ajv-formats@3.0.1";
import { validateHeartbeat, validateIncidentEvent } from "./contracts.ts";

const root = new URL("../../../packages/contracts/", import.meta.url);
const read = async (p: string) => JSON.parse(await Deno.readTextFile(new URL(p, root)));

// deno-lint-ignore no-explicit-any
const ajv = new (Ajv2020 as any)({ strict: false, allErrors: true });
// deno-lint-ignore no-explicit-any
(addFormats as any)(ajv);
const schemaEvent = ajv.compile(await read("schemas/incident-event.v1.schema.json"));
const schemaHb = ajv.compile(await read("schemas/heartbeat.v1.schema.json"));

// Real messages produced by the Python edge service (synthetic convulsion run).
const opened = await read("examples/incident_opened.json");
const ended = await read("examples/incident_ended.json");
const heartbeat = await read("examples/heartbeat.json");

function mutate(base: Record<string, unknown>, patch: Record<string, unknown>) {
  const copy = structuredClone(base);
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete copy[k];
    else copy[k] = v;
  }
  return copy;
}

const eventCases: [string, unknown][] = [
  ["edge example: opened", opened],
  ["edge example: ended", ended],
  [
    "minimal (optional fields absent)",
    mutate(opened, {
      follows_incident_id: undefined,
      ended_at: undefined,
      features: undefined,
    }),
  ],
  ["missing event_id", mutate(opened, { event_id: undefined })],
  ["bad uuid", mutate(opened, { incident_id: "not-a-uuid" })],
  ["unknown signal", mutate(opened, { signal_type: "seizure" })],
  ["confidence > 1", mutate(opened, { confidence: 1.2 })],
  ["confidence negative", mutate(opened, { confidence: -0.1 })],
  ["extra field", mutate(opened, { diagnosis: "epilepsy" })],
  ["bad timestamp", mutate(opened, { occurred_at: "yesterday" })],
  ["date only", mutate(opened, { started_at: "2026-11-20" })],
  ["offset timestamp", mutate(opened, { occurred_at: "2026-11-20T08:22:16.937+01:00" })],
  ["image data in features", mutate(opened, { features: { frame: [1, 2, 3] } })],
  ["empty rule_version", mutate(opened, { rule_version: "" })],
  ["wrong schema version", mutate(opened, { schema_version: "incident-event.v2" })],
  ["device_id too long", mutate(opened, { device_id: "x".repeat(65) })],
  ["not an object", [opened]],
];

for (const [name, payload] of eventCases) {
  Deno.test(`incident-event agrees with JSON Schema: ${name}`, () => {
    assertEquals(validateIncidentEvent(payload).ok, schemaEvent(payload) as boolean);
  });
}

const cam = heartbeat.camera as Record<string, unknown>;
const st = heartbeat.storage as Record<string, unknown>;
const hbCases: [string, unknown][] = [
  ["edge example", heartbeat],
  ["maintenance missing", mutate(heartbeat, { maintenance: undefined })],
  ["camera extra field", mutate(heartbeat, { camera: { ...cam, jpeg: "..." } })],
  ["negative fps", mutate(heartbeat, { camera: { ...cam, fps: -1 } })],
  ["fractional free bytes", mutate(heartbeat, { storage: { ...st, free_bytes: 1.5 } })],
  ["warnings not strings", mutate(heartbeat, { storage: { ...st, warnings: [1] } })],
  ["memory > 100", mutate(heartbeat, { memory_used_percent: 101 })],
  ["null temperature", mutate(heartbeat, { temperature_c: null })],
  ["extra top-level", mutate(heartbeat, { frame: "base64" })],
  ["negative queue", mutate(heartbeat, { queue_depth: -1 })],
];

for (const [name, payload] of hbCases) {
  Deno.test(`heartbeat agrees with JSON Schema: ${name}`, () => {
    assertEquals(validateHeartbeat(payload).ok, schemaHb(payload) as boolean);
  });
}

Deno.test("edge examples are valid", () => {
  assertEquals(validateIncidentEvent(opened).ok, true);
  assertEquals(validateIncidentEvent(ended).ok, true);
  assertEquals(validateHeartbeat(heartbeat).ok, true);
});
