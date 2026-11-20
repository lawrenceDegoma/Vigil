import { assert, assertEquals, assertMatch, assertStringIncludes } from "jsr:@std/assert@1.0.13";
import { localTime, maskPhone, type MessageContext, plain, renderMessage } from "./messages.ts";

const now = new Date("2026-11-20T05:45:00Z"); // 9:45 PM, Nov 19, in Los Angeles
const base: MessageContext = {
  kind: "incident_alert",
  round: 1,
  reminderRounds: 2,
  signalType: "convulsion",
  severity: "urgent",
  incidentId: "c815ad78-26fd-4124-8159-b201d281b02a",
  startedAt: new Date("2026-11-20T05:41:00Z"),
  deviceName: "Living room",
  subjectName: "Rex",
  timezone: "America/Los_Angeles",
  offlineSince: null,
  now,
  appUrl: "https://vigil.example.test/",
};
const GSM_SAFE = /^[\x20-\x7e]*$/;

Deno.test("urgent alert wording", () => {
  const m = renderMessage(base);
  assertEquals(
    m,
    "Vigil URGENT: possible seizure-like movement seen for Rex at 9:41 PM (Living room). " +
      "Please check now. https://vigil.example.test/incidents/c815ad78-26fd-4124-8159-b201d281b02a",
  );
  assertMatch(m, GSM_SAFE);
  assert(m.length <= 306, "fits in two SMS segments");
});

Deno.test("warnings never mention seizures or urgency", () => {
  const m = renderMessage({ ...base, signalType: "vomiting", severity: "warning" });
  assertStringIncludes(m, "possible vomiting");
  assert(!/seizure|urgent|imminent/i.test(m), m);
});

Deno.test("no message is a diagnosis or gives treatment advice", () => {
  const kinds = [
    "incident_alert",
    "incident_reminder",
    "device_offline",
    "device_recovered",
  ] as const;
  const signals = ["convulsion", "vomiting", "fall", "prolonged_inactivity"] as const;
  for (const kind of kinds) {
    for (const signalType of signals) {
      const m = renderMessage({ ...base, kind, round: 2, signalType, offlineSince: now });
      assert(!/diagnos|epilep|has a seizure|medicat|dose|give |treat/i.test(m), m);
      assertMatch(m, GSM_SAFE);
      assert(m.length <= 480);
    }
  }
});

Deno.test("reminder numbering and late reports", () => {
  const r = renderMessage({ ...base, kind: "incident_reminder", round: 3 });
  assertStringIncludes(r, "Vigil reminder 2/2:");
  assertStringIncludes(r, "has not been acknowledged");
  const late = renderMessage({ ...base, now: new Date("2026-11-20T06:30:00Z") });
  assertStringIncludes(late, "9:41 PM (reported late)");
});

Deno.test("times are local; other days include the date", () => {
  const la = "America/Los_Angeles";
  assertEquals(localTime(new Date("2026-11-20T05:41:00Z"), now, la), "9:41 PM");
  assertEquals(localTime(new Date("2026-11-18T05:41:00Z"), now, la), "Nov 17, 9:41 PM");
  assertEquals(localTime(new Date("2026-11-20T05:41:00Z"), now, "UTC"), "5:41 AM");
});

Deno.test("offline and recovery notices", () => {
  const since = new Date("2026-11-20T05:30:00Z");
  assertEquals(
    renderMessage({ ...base, kind: "device_offline", offlineSince: since }),
    "Vigil: Living room is OFFLINE. No heartbeat since 9:30 PM. Monitoring and alerts may not be working.",
  );
  assertEquals(
    renderMessage({ ...base, kind: "device_recovered", offlineSince: since }),
    "Vigil: Living room is back online (offline since 9:30 PM).",
  );
});

Deno.test("user-provided names are cleaned and bounded; no link without app URL", () => {
  const m = renderMessage({
    ...base,
    subjectName: "  Rex’s   pup ",
    deviceName: "x".repeat(200),
    appUrl: null,
  });
  assertStringIncludes(m, "for Rex's pup at");
  assert(!m.includes("http"));
  assert(m.length < 200);
  assertEquals(plain("9:41 PM"), "9:41 PM");
});

Deno.test("phones are masked for logs", () => {
  assertEquals(maskPhone("+15555550102"), "+1555***0102");
});
