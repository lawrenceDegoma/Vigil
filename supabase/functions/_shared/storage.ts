// Minimal Supabase Storage client (service role, server-side only). Paths returned
// to callers are relative to the project URL ("/storage/v1/..."), so the device and
// browser resolve them against the public URL they already know.

export interface ClipStorage {
  /** Relative signed upload URL (PUT the file there; overwrites allowed). */
  createUploadUrl(path: string): Promise<string>;
  /** Object size in bytes, or null when missing. */
  objectSize(path: string): Promise<number | null>;
  /** Relative signed read URL; `download` sets a Content-Disposition filename. */
  createSignedUrl(path: string, expiresIn: number, download?: string): Promise<string>;
  remove(paths: string[]): Promise<void>;
}

export class StorageError extends Error {}

export class SupabaseStorage implements ClipStorage {
  private readonly fetch: typeof fetch;

  constructor(
    private readonly projectUrl: string, // internal URL the function can reach
    private readonly serviceKey: string,
    readonly bucket = "incident-clips",
    fetchImpl?: typeof fetch,
  ) {
    this.fetch = fetchImpl ?? fetch;
  }

  static fromEnv(): SupabaseStorage {
    const url = Deno.env.get("SUPABASE_URL");
    const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
    if (!url || !key) throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing");
    return new SupabaseStorage(url, key);
  }

  private async call(method: string, rel: string, body?: unknown, extra: HeadersInit = {}) {
    const res = await this.fetch(`${this.projectUrl.replace(/\/+$/, "")}/storage/v1${rel}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.serviceKey}`,
        apikey: this.serviceKey,
        "Content-Type": "application/json",
        ...extra,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new StorageError(`storage ${method} ${res.status}`);
    return data;
  }

  private enc(path: string): string {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  async createUploadUrl(path: string): Promise<string> {
    const data = await this.call(
      "POST",
      `/object/upload/sign/${this.bucket}/${this.enc(path)}`,
      {},
      { "x-upsert": "true" },
    );
    if (typeof data?.url !== "string") throw new StorageError("no upload url");
    return `/storage/v1${data.url}`;
  }

  async objectSize(path: string): Promise<number | null> {
    const slash = path.lastIndexOf("/");
    const rows = await this.call("POST", `/object/list/${this.bucket}`, {
      prefix: path.slice(0, slash),
      search: path.slice(slash + 1),
      limit: 10,
      offset: 0,
    });
    const hit = Array.isArray(rows)
      ? rows.find((r: { name?: string }) => r.name === path.slice(slash + 1))
      : null;
    const size = hit?.metadata?.size;
    return typeof size === "number" ? size : null;
  }

  async createSignedUrl(path: string, expiresIn: number, download?: string): Promise<string> {
    const data = await this.call("POST", `/object/sign/${this.bucket}/${this.enc(path)}`, {
      expiresIn,
    });
    if (typeof data?.signedURL !== "string") throw new StorageError("no signed url");
    const dl = download === undefined ? "" : `&download=${encodeURIComponent(download)}`;
    return `/storage/v1${data.signedURL}${dl}`;
  }

  async remove(paths: string[]): Promise<void> {
    if (paths.length) await this.call("DELETE", `/object/${this.bucket}`, { prefixes: paths });
  }
}
