import {
  assert,
  assertEquals,
  assertInstanceOf,
  assertRejects,
  assertThrows,
} from "jsr:@std/assert@1.0.13";
import { MockSmsProvider, providerFromEnv, SmsError, TwilioSmsProvider } from "./sms.ts";

const SID = "AC" + "0".repeat(32);
const env = (vars: Record<string, string>) => (k: string) => vars[k];
const twilioVars = {
  SMS_PROVIDER: "twilio",
  TWILIO_ACCOUNT_SID: SID,
  TWILIO_AUTH_TOKEN: "t",
  TWILIO_FROM: "+15555550100",
};

Deno.test("mock is the default provider", () => {
  assertInstanceOf(providerFromEnv(env({}), () => {}), MockSmsProvider);
});

Deno.test("real SMS requires explicit owner approval", () => {
  assertThrows(() => providerFromEnv(env(twilioVars)), Error, "SMS_LIVE_APPROVED");
  assertInstanceOf(
    providerFromEnv(env({ ...twilioVars, SMS_LIVE_APPROVED: "yes" })),
    TwilioSmsProvider,
  );
});

Deno.test("twilio request format (no network)", async () => {
  let seenUrl = "";
  let seenInit: RequestInit = {};
  const fakeFetch = ((url: string, init: RequestInit) => {
    seenUrl = url;
    seenInit = init;
    return Promise.resolve(new Response(JSON.stringify({ sid: "SM123" }), { status: 201 }));
  }) as unknown as typeof fetch;
  const p = new TwilioSmsProvider({
    accountSid: SID,
    authToken: "tok",
    messagingServiceSid: "MG1",
    fetch: fakeFetch,
  });
  assertEquals(await p.send("+15555550102", "hello"), { id: "SM123" });
  assertEquals(seenUrl, `https://api.twilio.com/2010-04-01/Accounts/${SID}/Messages.json`);
  const headers = seenInit.headers as Record<string, string>;
  assertEquals(headers.Authorization, `Basic ${btoa(`${SID}:tok`)}`);
  const form = seenInit.body as URLSearchParams;
  assertEquals(form.get("To"), "+15555550102");
  assertEquals(form.get("Body"), "hello");
  assertEquals(form.get("MessagingServiceSid"), "MG1");
  assertEquals(form.get("From"), null);
});

Deno.test("twilio errors: 429/5xx/network retryable, other 4xx not", async () => {
  const respond = (status: number) =>
    (() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: 21211 }), { status }),
      )) as unknown as typeof fetch;
  const make = (f: typeof fetch) =>
    new TwilioSmsProvider({ accountSid: SID, authToken: "t", from: "+15555550100", fetch: f });
  const cases: [number, boolean][] = [[429, true], [503, true], [400, false], [401, false]];
  for (const [status, retryable] of cases) {
    const e = await assertRejects(() => make(respond(status)).send("+15555550102", "x"), SmsError);
    assertEquals(e.retryable, retryable, `status ${status}`);
  }
  const net = (() => Promise.reject(new TypeError("connection reset"))) as unknown as typeof fetch;
  const e = await assertRejects(() => make(net).send("+15555550102", "x"), SmsError);
  assert(e.retryable);
});
