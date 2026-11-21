import { assert, assertEquals, assertStringIncludes } from "jsr:@std/assert@1.0.13";
import {
  type ClipAccessRow,
  type ClipDb,
  type ClipRegistration,
  handleClipUpload,
  handleClipUrl,
  MAX_PART_BYTES,
  purgeExpiredClips,
  type RegisterResult,
  validateRegistration,
} from "./clips.ts";
import { type ClipStorage, SupabaseStorage } from "./storage.ts";

const CLIP = "c0000000-0000-0000-0000-000000000001";
const INC = "b0000000-0000-0000-0000-000000000001";
const sha = "a".repeat(64);
const reg: ClipRegistration = {
  id: CLIP,
  incident_ids: [INC],
  started_at: "2026-11-20T11:59:30Z",
  ended_at: "2026-11-20T12:03:00Z",
  bytes: 2000,
  sha256: sha,
  parts: [{ index: 0, bytes: 1000, sha256: sha }, { index: 1, bytes: 1000, sha256: sha }],
};

class FakeStorage implements ClipStorage {
  sizes = new Map<string, number>();
  removed: string[] = [];
  signed: { path: string; expires: number; download?: string }[] = [];
  createUploadUrl(path: string) {
    return Promise.resolve(`/storage/v1/object/upload/sign/incident-clips/${path}?token=t`);
  }
  objectSize(path: string) {
    return Promise.resolve(this.sizes.get(path) ?? null);
  }
  createSignedUrl(path: string, expires: number, download?: string) {
    this.signed.push({ path, expires, download });
    return Promise.resolve(`/storage/v1/object/sign/incident-clips/${path}?token=s`);
  }
  remove(paths: string[]) {
    this.removed.push(...paths);
    return Promise.resolve();
  }
}

class FakeClipDb implements ClipDb {
  audits: string[] = [];
  forgotten: string[] = [];
  completed: number[] = [];
  constructor(
    public register: RegisterResult = {
      status: "ok",
      parts: [
        { index: 0, path: "h/d/c/part-000.mp4", bytes: 1000, uploaded: true },
        { index: 1, path: "h/d/c/part-001.mp4", bytes: 1000, uploaded: false },
      ],
    },
    public access: ClipAccessRow[] = [],
  ) {}
  registerClip() {
    return Promise.resolve(this.register);
  }
  completeClipPart(_d: string, _c: string, index: number) {
    this.completed.push(index);
    return Promise.resolve({ status: "ok", complete: true });
  }
  clipPartPath(_d: string, _c: string, index: number) {
    return Promise.resolve({ storage_path: `h/d/c/part-00${index}.mp4`, bytes: 1000 });
  }
  clipAccess() {
    return Promise.resolve(this.access);
  }
  audit(_h: string, _a: string, action: string) {
    this.audits.push(action);
    return Promise.resolve();
  }
  expiredClips() {
    return Promise.resolve([
      { clip_id: "x", storage_path: "h/d/x/part-000.mp4" },
      { clip_id: "x", storage_path: "h/d/x/part-001.mp4" },
    ]);
  }
  forgetClip(id: string) {
    this.forgotten.push(id);
    return Promise.resolve();
  }
}

const now = new Date("2026-11-20T12:05:00Z");

Deno.test("registration validation", () => {
  assertEquals(typeof validateRegistration(reg), "object");
  const bad: [string, unknown][] = [
    ["bad clip id", { ...reg, id: "x" }],
    ["bad incident_ids", { ...reg, incident_ids: [] }],
    ["parts must be numbered 0..n-1 in order", { ...reg, parts: [reg.parts[1]] }],
    ["part 0 too large or empty", {
      ...reg,
      parts: [{ index: 0, bytes: MAX_PART_BYTES + 1, sha256: sha }],
    }],
    ["bad sha256", { ...reg, sha256: "nope" }],
  ];
  for (const [msg, c] of bad) assertEquals(validateRegistration(c), msg);
});

Deno.test("register returns upload URLs only for parts not yet uploaded", async () => {
  const r = await handleClipUpload(
    new FakeClipDb(),
    new FakeStorage(),
    "dev",
    { action: "register", clip: reg },
    now,
  );
  assertEquals(r.status, 200);
  const parts = r.body.parts as { index: number; upload_url: string | null }[];
  assertEquals(parts[0].upload_url, null);
  assertStringIncludes(parts[1].upload_url!, "/storage/v1/object/upload/sign/incident-clips/");
});

Deno.test("ineligible clips get no URLs", async () => {
  const r = await handleClipUpload(
    new FakeClipDb({ status: "not_eligible" }),
    new FakeStorage(),
    "dev",
    { action: "register", clip: reg },
    now,
  );
  assertEquals(r.body, { status: "not_eligible", reason: undefined });
});

Deno.test("complete checks the stored object's size", async () => {
  const db = new FakeClipDb();
  const st = new FakeStorage();
  const complete = { action: "complete", clip_id: CLIP, part_index: 1 };
  assertEquals((await handleClipUpload(db, st, "dev", complete, now)).body.status, "missing");
  st.sizes.set("h/d/c/part-001.mp4", 999);
  assertEquals((await handleClipUpload(db, st, "dev", complete, now)).body.status, "size_mismatch");
  st.sizes.set("h/d/c/part-001.mp4", 1000);
  const ok = await handleClipUpload(db, st, "dev", complete, now);
  assertEquals(ok.body, { status: "ok", complete: true });
  assertEquals(db.completed, [1]);
});

Deno.test("viewing: members get short-lived URLs; only admins may download; audited", async () => {
  const rows = (role: "admin" | "family"): ClipAccessRow[] => [
    { household_id: "h", role, part_index: 0, storage_path: "h/d/c/part-000.mp4" },
    { household_id: "h", role, part_index: 1, storage_path: "h/d/c/part-001.mp4" },
  ];
  const famDb = new FakeClipDb(undefined, rows("family"));
  const st = new FakeStorage();
  const view = await handleClipUrl(famDb, st, "u", { clip_id: CLIP });
  assertEquals(view.status, 200);
  assertEquals((view.body.parts as unknown[]).length, 2);
  assert(st.signed.every((s) => s.expires === 300 && s.download === undefined));
  assertEquals(
    (await handleClipUrl(famDb, st, "u", { clip_id: CLIP, download: true })).status,
    403,
  );
  assertEquals(famDb.audits, ["clip_viewed"]);

  const adminDb = new FakeClipDb(undefined, rows("admin"));
  const dl = await handleClipUrl(adminDb, st, "a", { clip_id: CLIP, download: true });
  assertEquals(dl.status, 200);
  assertEquals(st.signed.at(-1)!.download, "vigil-c0000000-part2.mp4");
  assertEquals(adminDb.audits, ["clip_downloaded"]);

  const none = await handleClipUrl(new FakeClipDb(), st, "x", { clip_id: CLIP });
  assertEquals(none.status, 404);
});

Deno.test("expired clips: objects removed, then rows forgotten once per clip", async () => {
  const db = new FakeClipDb();
  const st = new FakeStorage();
  assertEquals(await purgeExpiredClips(db, st, now), 1);
  assertEquals(st.removed.length, 2);
  assertEquals(db.forgotten, ["x"]);
});

Deno.test("storage client request formats (no network)", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const replies: unknown[] = [
    { url: "/object/upload/sign/incident-clips/h/d/c/part-000.mp4?token=u" },
    [{ name: "part-000.mp4", metadata: { size: 1234 } }],
    { signedURL: "/object/sign/incident-clips/h/d/c/part-000.mp4?token=s" },
    {},
  ];
  const fake = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(new Response(JSON.stringify(replies.shift()), { status: 200 }));
  }) as unknown as typeof fetch;
  const s = new SupabaseStorage("http://kong:8000/", "service-key", "incident-clips", fake);
  assertEquals(
    await s.createUploadUrl("h/d/c/part-000.mp4"),
    "/storage/v1/object/upload/sign/incident-clips/h/d/c/part-000.mp4?token=u",
  );
  assertEquals((calls[0].init.headers as Record<string, string>)["x-upsert"], "true");
  assertEquals(await s.objectSize("h/d/c/part-000.mp4"), 1234);
  assertEquals(JSON.parse(calls[1].init.body as string).prefix, "h/d/c");
  assertEquals(
    await s.createSignedUrl("h/d/c/part-000.mp4", 300, "v.mp4"),
    "/storage/v1/object/sign/incident-clips/h/d/c/part-000.mp4?token=s&download=v.mp4",
  );
  await s.remove(["h/d/c/part-000.mp4"]);
  assertEquals(calls[3].init.method, "DELETE");
  assertEquals(calls.map((c) => c.url.split("/storage/v1")[0]), Array(4).fill("http://kong:8000"));
  assert(
    calls.every((c) =>
      (c.init.headers as Record<string, string>).Authorization === "Bearer service-key"
    ),
  );
});
