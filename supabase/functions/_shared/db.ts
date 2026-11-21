// Postgres access for Edge Functions. Uses the direct database URL that the
// Supabase runtime provides (SUPABASE_DB_URL) and only calls `private.*`
// functions; no table is written directly from TypeScript.

import postgres from "npm:postgres@3.4.9";
import type { ClaimedNotification, Completion, EngineDb } from "./dispatcher.ts";
import type { ClipAccessRow, ClipDb, ClipRegistration, RegisterResult } from "./clips.ts";
import type { Json } from "./contracts.ts";

export interface Device {
  id: string;
  household_id: string;
  device_key: string;
}

export type IngestResult = {
  status: "accepted" | "duplicate" | "rejected";
  reason?: string;
  notifications?: number;
  [k: string]: unknown;
};

export class Db implements EngineDb, ClipDb {
  readonly sql: postgres.Sql;

  constructor(url: string) {
    // prepare:false keeps us compatible with the transaction pooler.
    this.sql = postgres(url, { max: 3, prepare: false, idle_timeout: 20 });
  }

  static fromEnv(): Db {
    const url = Deno.env.get("SUPABASE_DB_URL");
    if (!url) throw new Error("SUPABASE_DB_URL is not set");
    return new Db(url);
  }

  async authenticateDevice(tokenHash: string): Promise<Device | null> {
    const rows = await this.sql<Device[]>`
      select id, household_id, device_key from private.authenticate_device(${tokenHash})`;
    return rows[0] ?? null;
  }

  async ingestIncidentEvent(deviceId: string, event: Json, now: Date): Promise<IngestResult> {
    const [row] = await this.sql`
      select private.ingest_incident_event(${deviceId}::uuid, ${this.sql.json(event as never)},
                                           ${now}) as r`;
    return row.r as IngestResult;
  }

  async ingestHeartbeat(deviceId: string, heartbeat: Json, now: Date): Promise<IngestResult> {
    const [row] = await this.sql`
      select private.ingest_heartbeat(${deviceId}::uuid, ${this.sql.json(heartbeat as never)},
                                      ${now}) as r`;
    return row.r as IngestResult;
  }

  async ingestTestAlert(deviceId: string, request: Json, now: Date): Promise<IngestResult> {
    const [row] = await this.sql`
      select private.ingest_test_alert(${deviceId}::uuid, ${this.sql.json(request as never)},
                                       ${now}) as r`;
    return row.r as IngestResult;
  }

  async scheduleReminders(now: Date): Promise<number> {
    const [row] = await this.sql`select private.schedule_reminders(${now}) as n`;
    return Number(row.n);
  }

  async checkStaleDevices(now: Date): Promise<number> {
    const [row] = await this.sql`select private.check_stale_devices(${now}) as n`;
    return Number(row.n);
  }

  async pruneHealth(now: Date): Promise<number> {
    const [row] = await this.sql`select private.prune_device_health(${now}) as n`;
    return Number(row.n);
  }

  async claim(now: Date, limit: number, leaseSeconds: number): Promise<ClaimedNotification[]> {
    const rows = await this.sql`
      select * from private.claim_notifications(${now}, ${limit}, ${leaseSeconds})`;
    return rows.map((r) => ({
      id: Number(r.id),
      kind: r.kind,
      round: Number(r.round),
      to_e164: r.to_e164,
      attempts: Number(r.attempts),
      incident_id: r.incident_id,
      signal_type: r.signal_type,
      severity: r.severity,
      started_at: r.started_at ? new Date(r.started_at) : null,
      device_name: r.device_name,
      subject_name: r.subject_name,
      timezone: r.timezone,
      reminder_rounds: Number(r.reminder_rounds),
      offline_since: r.offline_since ? new Date(r.offline_since) : null,
    }));
  }

  async complete(id: number, c: Completion, now: Date): Promise<void> {
    await this.sql`
      select private.complete_notification(${id}, ${c.ok}, ${c.provider}, ${c.providerMessageId},
                                            ${c.body}, ${c.error}, ${c.retryAt}, ${now})`;
  }

  // ------------------------------------------------------------------ clips

  async registerClip(deviceId: string, clip: ClipRegistration, now: Date): Promise<RegisterResult> {
    const [row] = await this.sql`
      select private.register_clip(${deviceId}::uuid, ${
      this.sql.json(clip as never)
    }, ${now}) as r`;
    return row.r as RegisterResult;
  }

  async completeClipPart(deviceId: string, clipId: string, index: number, now: Date) {
    const [row] = await this.sql`
      select private.complete_clip_part(${deviceId}::uuid, ${clipId}::uuid, ${index}::smallint,
                                        ${now}) as r`;
    return row.r as { status: string; complete?: boolean; reason?: string };
  }

  async clipPartPath(deviceId: string, clipId: string, index: number) {
    const rows = await this.sql<{ storage_path: string; bytes: number }[]>`
      select storage_path, bytes from private.clip_part_path(${deviceId}::uuid, ${clipId}::uuid,
                                                             ${index}::smallint)`;
    return rows[0] ? { storage_path: rows[0].storage_path, bytes: Number(rows[0].bytes) } : null;
  }

  async clipAccess(userId: string, clipId: string): Promise<ClipAccessRow[]> {
    const rows = await this.sql`
      select household_id, role, part_index, storage_path
        from private.clip_access(${userId}::uuid, ${clipId}::uuid)`;
    return rows.map((r) => ({
      household_id: r.household_id,
      role: r.role,
      part_index: Number(r.part_index),
      storage_path: r.storage_path,
    }));
  }

  async audit(
    household: string,
    actor: string,
    action: string,
    type: string,
    target: string,
    meta: Record<string, unknown> = {},
  ): Promise<void> {
    await this.sql`select private.audit(${household}::uuid, ${actor}::uuid, ${action}, ${type},
                                        ${target}, ${this.sql.json(meta as never)})`;
  }

  async expiredClips(now: Date, limit: number) {
    return await this.sql<{ clip_id: string; storage_path: string }[]>`
      select clip_id, storage_path from private.expired_clips(${now}, ${limit})`;
  }

  async forgetClip(clipId: string): Promise<void> {
    await this.sql`select private.forget_clip(${clipId}::uuid)`;
  }
}
