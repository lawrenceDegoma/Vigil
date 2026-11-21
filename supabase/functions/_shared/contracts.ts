// Validation of device messages against packages/contracts (incident-event.v1, heartbeat.v1).
// Hand-written so the Edge runtime needs no schema compiler; contracts_test.ts
// cross-checks every fixture against the JSON Schemas with Ajv.

export type Json = Record<string, unknown>;
export type Result<T> = { ok: true; value: T } | { ok: false; error: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_TIME = /^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$/;

export const SIGNAL_TYPES = ["convulsion", "vomiting", "fall", "prolonged_inactivity"] as const;
export type SignalType = typeof SIGNAL_TYPES[number];
export const SEVERITIES = ["warning", "urgent"] as const;
export const EVENT_TYPES = ["incident_opened", "incident_updated", "incident_ended"] as const;

export interface IncidentEventV1 extends Json {
  schema_version: "incident-event.v1";
  event_id: string;
  device_id: string;
  incident_id: string;
  event_type: typeof EVENT_TYPES[number];
  signal_type: SignalType;
  severity: typeof SEVERITIES[number];
  occurred_at: string;
  started_at: string;
  confidence: number;
  rule_version: string;
}

export interface HeartbeatV1 extends Json {
  schema_version: "heartbeat.v1";
  device_id: string;
  sent_at: string;
}

class Invalid extends Error {}

const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

function check(cond: boolean, msg: string): void {
  if (!cond) throw new Invalid(msg);
}

function onlyKeys(o: Json, allowed: readonly string[], where: string): void {
  for (const k of Object.keys(o)) check(allowed.includes(k), `${where}: unexpected field ${k}`);
}

function required(o: Json, keys: readonly string[], where: string): void {
  for (const k of keys) check(k in o, `${where}: missing ${k}`);
}

const str = (v: unknown, min = 0, max = Infinity) =>
  typeof v === "string" && v.length >= min && v.length <= max;
const uuid = (v: unknown) => typeof v === "string" && UUID.test(v);
const dateTime = (v: unknown) =>
  typeof v === "string" && DATE_TIME.test(v) && !Number.isNaN(Date.parse(v));
const num = (v: unknown, min = -Infinity, max = Infinity) =>
  typeof v === "number" && Number.isFinite(v) && v >= min && v <= max;
const int = (v: unknown, min = -Infinity) => Number.isInteger(v) && (v as number) >= min;
const nullable = (v: unknown, test: (x: unknown) => boolean) => v === null || test(v);

function wrap<T>(fn: () => T): Result<T> {
  try {
    return { ok: true, value: fn() };
  } catch (e) {
    if (e instanceof Invalid) return { ok: false, error: e.message };
    throw e;
  }
}

const EVENT_FIELDS = [
  "schema_version",
  "event_id",
  "device_id",
  "incident_id",
  "follows_incident_id",
  "event_type",
  "signal_type",
  "severity",
  "occurred_at",
  "started_at",
  "ended_at",
  "confidence",
  "rule_version",
  "features",
] as const;

export function validateIncidentEvent(v: unknown): Result<IncidentEventV1> {
  return wrap(() => {
    const w = "incident-event.v1";
    check(isObj(v), `${w}: not an object`);
    const e = v as Json;
    onlyKeys(e, EVENT_FIELDS, w);
    required(
      e,
      EVENT_FIELDS.filter((k) => !["follows_incident_id", "ended_at", "features"].includes(k)),
      w,
    );
    check(e.schema_version === "incident-event.v1", `${w}: wrong schema_version`);
    check(uuid(e.event_id), `${w}: event_id must be a uuid`);
    check(str(e.device_id, 1, 64), `${w}: bad device_id`);
    check(uuid(e.incident_id), `${w}: incident_id must be a uuid`);
    if ("follows_incident_id" in e) {
      check(nullable(e.follows_incident_id, uuid), `${w}: bad follows_incident_id`);
    }
    check(EVENT_TYPES.includes(e.event_type as never), `${w}: bad event_type`);
    check(SIGNAL_TYPES.includes(e.signal_type as never), `${w}: bad signal_type`);
    check(SEVERITIES.includes(e.severity as never), `${w}: bad severity`);
    check(dateTime(e.occurred_at), `${w}: bad occurred_at`);
    check(dateTime(e.started_at), `${w}: bad started_at`);
    if ("ended_at" in e) check(nullable(e.ended_at, dateTime), `${w}: bad ended_at`);
    check(num(e.confidence, 0, 1), `${w}: confidence must be within [0, 1]`);
    check(str(e.rule_version, 1), `${w}: bad rule_version`);
    if ("features" in e) {
      check(isObj(e.features), `${w}: features must be an object`);
      for (const [k, f] of Object.entries(e.features as Json)) {
        check(
          f === null || typeof f === "string" || typeof f === "boolean" || num(f),
          `${w}: feature ${k} must be a number, string, boolean or null`,
        );
      }
    }
    return e as IncidentEventV1;
  });
}

const HB_FIELDS = [
  "schema_version",
  "device_id",
  "sent_at",
  "software_version",
  "maintenance",
  "camera",
  "detector",
  "storage",
  "queue_depth",
  "temperature_c",
  "cpu_percent",
  "memory_used_percent",
] as const;

export function validateHeartbeat(v: unknown): Result<HeartbeatV1> {
  return wrap(() => {
    const w = "heartbeat.v1";
    check(isObj(v), `${w}: not an object`);
    const h = v as Json;
    onlyKeys(h, HB_FIELDS, w);
    required(h, HB_FIELDS.slice(0, 9), w);
    check(h.schema_version === "heartbeat.v1", `${w}: wrong schema_version`);
    check(str(h.device_id, 1, 64), `${w}: bad device_id`);
    check(dateTime(h.sent_at), `${w}: bad sent_at`);
    check(typeof h.software_version === "string", `${w}: bad software_version`);
    check(typeof h.maintenance === "boolean", `${w}: bad maintenance`);

    check(isObj(h.camera), `${w}: camera must be an object`);
    const cam = h.camera as Json;
    onlyKeys(cam, ["ok", "last_frame_at", "fps", "dropped_frames", "error"], `${w}.camera`);
    required(cam, ["ok", "last_frame_at", "fps"], `${w}.camera`);
    check(typeof cam.ok === "boolean", `${w}.camera: bad ok`);
    check(nullable(cam.last_frame_at, dateTime), `${w}.camera: bad last_frame_at`);
    check(num(cam.fps, 0), `${w}.camera: bad fps`);
    if ("dropped_frames" in cam) {
      check(int(cam.dropped_frames, 0), `${w}.camera: bad dropped_frames`);
    }
    if ("error" in cam) {
      check(nullable(cam.error, (x) => typeof x === "string"), `${w}.camera: bad error`);
    }

    check(isObj(h.detector), `${w}: detector must be an object`);
    const det = h.detector as Json;
    onlyKeys(
      det,
      ["ok", "last_window_at", "processing_fps", "rule_version", "error"],
      `${w}.detector`,
    );
    required(det, ["ok", "last_window_at", "processing_fps"], `${w}.detector`);
    check(typeof det.ok === "boolean", `${w}.detector: bad ok`);
    check(nullable(det.last_window_at, dateTime), `${w}.detector: bad last_window_at`);
    check(num(det.processing_fps, 0), `${w}.detector: bad processing_fps`);
    if ("rule_version" in det) {
      check(typeof det.rule_version === "string", `${w}.detector: bad rule_version`);
    }
    if ("error" in det) {
      check(nullable(det.error, (x) => typeof x === "string"), `${w}.detector: bad error`);
    }

    check(isObj(h.storage), `${w}: storage must be an object`);
    const st = h.storage as Json;
    onlyKeys(st, ["free_bytes", "total_bytes", "recordings_bytes", "warnings"], `${w}.storage`);
    required(st, ["free_bytes", "total_bytes"], `${w}.storage`);
    check(int(st.free_bytes, 0) && int(st.total_bytes, 0), `${w}.storage: bad byte counts`);
    if ("recordings_bytes" in st) {
      check(int(st.recordings_bytes, 0), `${w}.storage: bad recordings_bytes`);
    }
    if ("warnings" in st) {
      check(
        Array.isArray(st.warnings) && st.warnings.every((x) => str(x, 0, 300)),
        `${w}.storage: bad warnings`,
      );
    }

    check(int(h.queue_depth, 0), `${w}: bad queue_depth`);
    if ("temperature_c" in h) check(nullable(h.temperature_c, num), `${w}: bad temperature_c`);
    if ("cpu_percent" in h) {
      check(nullable(h.cpu_percent, (x) => num(x, 0)), `${w}: bad cpu_percent`);
    }
    if ("memory_used_percent" in h) {
      check(
        nullable(h.memory_used_percent, (x) => num(x, 0, 100)),
        `${w}: bad memory_used_percent`,
      );
    }
    return h as HeartbeatV1;
  });
}

export interface TestAlertV1 extends Json {
  schema_version: "test-alert.v1";
  request_id: string;
  device_id: string;
  requested_at: string;
  scope: "all" | "admin";
}

const TEST_FIELDS = ["schema_version", "request_id", "device_id", "requested_at", "scope"] as const;

export function validateTestAlert(v: unknown): Result<TestAlertV1> {
  return wrap(() => {
    const w = "test-alert.v1";
    check(isObj(v), `${w}: not an object`);
    const t = v as Json;
    onlyKeys(t, TEST_FIELDS, w);
    required(t, TEST_FIELDS, w);
    check(t.schema_version === "test-alert.v1", `${w}: wrong schema_version`);
    check(uuid(t.request_id), `${w}: request_id must be a uuid`);
    check(str(t.device_id, 1, 64), `${w}: bad device_id`);
    check(dateTime(t.requested_at), `${w}: bad requested_at`);
    check(t.scope === "all" || t.scope === "admin", `${w}: scope must be all or admin`);
    return t as TestAlertV1;
  });
}
