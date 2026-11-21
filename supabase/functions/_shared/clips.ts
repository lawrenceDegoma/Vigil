// Clip upload (device side) and viewing (user side), ADR-010 option (b).
// Pure handlers over small interfaces; the HTTP entry points are thin wrappers.

import type { ClipStorage } from "./storage.ts";

export const MAX_PART_BYTES = 50 * 1024 * 1024;
export const VIEW_URL_SECONDS = 300;

export interface ClipDb {
  registerClip(deviceId: string, clip: ClipRegistration, now: Date): Promise<RegisterResult>;
  completeClipPart(deviceId: string, clipId: string, index: number, now: Date): Promise<
    { status: string; complete?: boolean; reason?: string }
  >;
  clipPartPath(
    deviceId: string,
    clipId: string,
    index: number,
  ): Promise<{ storage_path: string; bytes: number } | null>;
  clipAccess(userId: string, clipId: string): Promise<ClipAccessRow[]>;
  audit(
    household: string,
    actor: string,
    action: string,
    type: string,
    target: string,
    meta?: Record<string, unknown>,
  ): Promise<void>;
  expiredClips(now: Date, limit: number): Promise<{ clip_id: string; storage_path: string }[]>;
  forgetClip(clipId: string): Promise<void>;
}

export interface ClipRegistration {
  id: string;
  incident_ids: string[];
  started_at: string;
  ended_at: string;
  bytes: number;
  sha256: string;
  parts: { index: number; bytes: number; sha256: string }[];
}

export interface RegisterResult {
  status: string;
  reason?: string;
  parts?: { index: number; path: string; bytes: number; uploaded: boolean }[];
}

export interface ClipAccessRow {
  household_id: string;
  role: "admin" | "family";
  part_index: number;
  storage_path: string;
}

export interface Reply {
  status: number;
  body: Record<string, unknown>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{64}$/;
const isTime = (v: unknown) => typeof v === "string" && !Number.isNaN(Date.parse(v));
const isInt = (v: unknown, min: number, max: number) =>
  Number.isInteger(v) && (v as number) >= min && (v as number) <= max;

export function validateRegistration(v: unknown): ClipRegistration | string {
  const c = v as Partial<ClipRegistration> | null;
  if (!c || typeof c !== "object") return "clip must be an object";
  if (typeof c.id !== "string" || !UUID.test(c.id)) return "bad clip id";
  if (
    !Array.isArray(c.incident_ids) || c.incident_ids.length < 1 || c.incident_ids.length > 20 ||
    !c.incident_ids.every((i) => typeof i === "string" && UUID.test(i))
  ) return "bad incident_ids";
  if (!isTime(c.started_at) || !isTime(c.ended_at)) return "bad timestamps";
  if (!isInt(c.bytes, 1, Number.MAX_SAFE_INTEGER)) return "bad bytes";
  if (typeof c.sha256 !== "string" || !SHA.test(c.sha256)) return "bad sha256";
  if (!Array.isArray(c.parts) || c.parts.length < 1 || c.parts.length > 50) return "bad parts";
  for (const [i, p] of c.parts.entries()) {
    if (!p || p.index !== i) return "parts must be numbered 0..n-1 in order";
    if (!isInt(p.bytes, 1, MAX_PART_BYTES)) return `part ${i} too large or empty`;
    if (typeof p.sha256 !== "string" || !SHA.test(p.sha256)) return `part ${i} bad sha256`;
  }
  return c as ClipRegistration;
}

/** Device: {action:"register", clip} or {action:"complete", clip_id, part_index}. */
export async function handleClipUpload(
  db: ClipDb,
  storage: ClipStorage,
  deviceId: string,
  body: unknown,
  now: Date,
): Promise<Reply> {
  const b = (body ?? {}) as Record<string, unknown>;
  if (b.action === "register") {
    const clip = validateRegistration(b.clip);
    if (typeof clip === "string") return { status: 400, body: { error: clip } };
    const r = await db.registerClip(deviceId, clip, now);
    if (r.status !== "ok") return { status: 200, body: { status: r.status, reason: r.reason } };
    const parts = [];
    for (const p of r.parts ?? []) {
      parts.push({
        index: p.index,
        upload_url: p.uploaded ? null : await storage.createUploadUrl(p.path),
      });
    }
    return { status: 200, body: { status: "ok", parts } };
  }
  if (b.action === "complete") {
    if (typeof b.clip_id !== "string" || !UUID.test(b.clip_id) || !isInt(b.part_index, 0, 49)) {
      return { status: 400, body: { error: "bad clip_id/part_index" } };
    }
    const part = await db.clipPartPath(deviceId, b.clip_id, b.part_index as number);
    if (!part) return { status: 404, body: { status: "unknown_part" } };
    const size = await storage.objectSize(part.storage_path);
    if (size === null) return { status: 409, body: { status: "missing" } };
    if (size !== Number(part.bytes)) return { status: 409, body: { status: "size_mismatch" } };
    const r = await db.completeClipPart(deviceId, b.clip_id, b.part_index as number, now);
    return { status: 200, body: r };
  }
  return { status: 400, body: { error: "unknown action" } };
}

/** User: {clip_id, download?}. Family members view; only admins get download links. */
export async function handleClipUrl(
  db: ClipDb,
  storage: ClipStorage,
  userId: string,
  body: unknown,
): Promise<Reply> {
  const b = (body ?? {}) as Record<string, unknown>;
  if (typeof b.clip_id !== "string" || !UUID.test(b.clip_id)) {
    return { status: 400, body: { error: "bad clip_id" } };
  }
  const download = b.download === true;
  const rows = await db.clipAccess(userId, b.clip_id);
  if (rows.length === 0) return { status: 404, body: { error: "not found" } };
  if (download && rows[0].role !== "admin") {
    return { status: 403, body: { error: "only administrators can download clips" } };
  }
  const parts = [];
  for (const r of rows) {
    const name = download
      ? `vigil-${b.clip_id.slice(0, 8)}-part${r.part_index + 1}.mp4`
      : undefined;
    parts.push({
      index: r.part_index,
      url: await storage.createSignedUrl(r.storage_path, VIEW_URL_SECONDS, name),
    });
  }
  await db.audit(
    rows[0].household_id,
    userId,
    download ? "clip_downloaded" : "clip_viewed",
    "clip",
    b.clip_id,
  );
  return { status: 200, body: { expires_in: VIEW_URL_SECONDS, parts } };
}

/** Cloud retention: delete expired, unpreserved clips (objects first, then rows). */
export async function purgeExpiredClips(
  db: ClipDb,
  storage: ClipStorage,
  now: Date,
): Promise<number> {
  const rows = await db.expiredClips(now, 50);
  if (rows.length === 0) return 0;
  await storage.remove(rows.map((r) => r.storage_path));
  const ids = [...new Set(rows.map((r) => r.clip_id))];
  for (const id of ids) await db.forgetClip(id);
  return ids.length;
}
