// Device ingestion, separated from the HTTP entry point for testing.
// One request carries up to MAX_MESSAGES outbox rows from the Pi; each message
// is validated against its contract and applied independently, and the reply
// lists a result per message so the device knows which rows it may mark sent.

import {
  type Json,
  validateHeartbeat,
  validateIncidentEvent,
  validateTestAlert,
} from "./contracts.ts";
import type { Device, IngestResult } from "./db.ts";

export const MAX_MESSAGES = 100;
export const MAX_BODY_BYTES = 512 * 1024;

export interface IngestDb {
  ingestIncidentEvent(deviceId: string, event: Json, now: Date): Promise<IngestResult>;
  ingestHeartbeat(deviceId: string, heartbeat: Json, now: Date): Promise<IngestResult>;
  ingestTestAlert(deviceId: string, request: Json, now: Date): Promise<IngestResult>;
}

export interface MessageResult {
  index: number;
  status: "accepted" | "duplicate" | "rejected";
  reason?: string;
}

export interface IngestReport {
  results: MessageResult[];
  notificationsQueued: number;
}

export class BadRequest extends Error {}

export async function ingestBatch(
  db: IngestDb,
  device: Device,
  body: unknown,
  now: Date,
): Promise<IngestReport> {
  const messages = (body as { messages?: unknown })?.messages;
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > MAX_MESSAGES) {
    throw new BadRequest(`expected {"messages": [...]} with 1-${MAX_MESSAGES} items`);
  }
  const results: MessageResult[] = [];
  let queued = 0;
  for (const [index, m] of messages.entries()) {
    const { kind, payload } = (m ?? {}) as { kind?: unknown; payload?: unknown };
    let r: IngestResult;
    if (kind === "incident_event") {
      const v = validateIncidentEvent(payload);
      r = v.ok
        ? await db.ingestIncidentEvent(device.id, v.value, now)
        : { status: "rejected", reason: v.error };
    } else if (kind === "heartbeat") {
      const v = validateHeartbeat(payload);
      r = v.ok
        ? await db.ingestHeartbeat(device.id, v.value, now)
        : { status: "rejected", reason: v.error };
    } else if (kind === "test_alert") {
      const v = validateTestAlert(payload);
      r = v.ok
        ? await db.ingestTestAlert(device.id, v.value, now)
        : { status: "rejected", reason: v.error };
    } else {
      r = { status: "rejected", reason: "unknown message kind" };
    }
    queued += Number(r.notifications ?? 0);
    results.push({ index, status: r.status, ...(r.reason ? { reason: r.reason } : {}) });
  }
  return { results, notificationsQueued: queued };
}
