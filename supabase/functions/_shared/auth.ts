// Device tokens: `vgl_` + 43 base64url chars (32 random bytes). Only the SHA-256
// hex digest is stored server-side; the token itself lives in the device's
// /etc/vigil/vigil.env (VIGIL_DEVICE_TOKEN).

const TOKEN = /^vgl_[A-Za-z0-9_-]{43}$/;

export function bearerToken(req: Request): string | null {
  const m = /^Bearer (\S+)$/.exec(req.headers.get("authorization") ?? "");
  return m && TOKEN.test(m[1]) ? m[1] : null;
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function newDeviceToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const b64 = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_")
    .replace(/=+$/, "");
  return `vgl_${b64}`;
}

/** Constant-time string comparison for shared secrets. */
export function safeEqual(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}
