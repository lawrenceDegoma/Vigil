import { assert, assertEquals, assertMatch } from "jsr:@std/assert@1.0.13";
import { bearerToken, newDeviceToken, safeEqual, sha256Hex } from "./auth.ts";

Deno.test("sha256 known vector", async () => {
  assertEquals(
    await sha256Hex("abc"),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  );
});

Deno.test("device tokens: format, uniqueness, bearer parsing", () => {
  const t = newDeviceToken();
  assertMatch(t, /^vgl_[A-Za-z0-9_-]{43}$/);
  assert(t !== newDeviceToken());
  const req = (h: string) => new Request("http://x", { headers: { authorization: h } });
  assertEquals(bearerToken(req(`Bearer ${t}`)), t);
  assertEquals(bearerToken(req("Bearer not-a-token")), null);
  assertEquals(bearerToken(req(`Basic ${t}`)), null);
});

Deno.test("safeEqual", () => {
  assert(safeEqual("abc", "abc"));
  assert(!safeEqual("abc", "abd"));
  assert(!safeEqual("abc", "abcd"));
});
