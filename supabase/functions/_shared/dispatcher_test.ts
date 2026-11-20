import { assert, assertEquals } from "jsr:@std/assert@1.0.13";
import {
  backoffSeconds,
  type ClaimedNotification,
  type Completion,
  dispatch,
  type EngineDb,
} from "./dispatcher.ts";
import { MockSmsProvider, SmsError, type SmsProvider } from "./sms.ts";

function claimed(
  id: number,
  to: string,
  extra: Partial<ClaimedNotification> = {},
): ClaimedNotification {
  return {
    id,
    kind: "incident_alert",
    round: 1,
    to_e164: to,
    attempts: 1,
    incident_id: "c815ad78-26fd-4124-8159-b201d281b02a",
    signal_type: "convulsion",
    severity: "urgent",
    started_at: new Date("2026-11-20T05:41:00Z"),
    device_name: "Living room",
    subject_name: "Rex",
    timezone: "America/Los_Angeles",
    reminder_rounds: 2,
    offline_since: null,
    ...extra,
  };
}

class FakeDb implements EngineDb {
  completions = new Map<number, Completion>();
  periodicCalls = 0;
  constructor(private batches: ClaimedNotification[][]) {}
  scheduleReminders() {
    this.periodicCalls++;
    return Promise.resolve(0);
  }
  checkStaleDevices() {
    this.periodicCalls++;
    return Promise.resolve(0);
  }
  claim() {
    return Promise.resolve(this.batches.shift() ?? []);
  }
  complete(id: number, c: Completion) {
    this.completions.set(id, c);
    return Promise.resolve();
  }
}

const now = () => new Date("2026-11-20T05:42:00Z");
const quiet = () => {};

Deno.test("every recipient is sent to concurrently", async () => {
  const started: string[] = [];
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const provider: SmsProvider = {
    name: "probe",
    async send(to) {
      started.push(to);
      if (started.length === 3) release();
      await gate; // nobody finishes until everyone has started
      return { id: `p-${to}` };
    },
  };
  const db = new FakeDb([[
    claimed(1, "+15555550101"),
    claimed(2, "+15555550102"),
    claimed(3, "+15555550103"),
  ]]);
  const report = await dispatch(db, provider, { now, log: quiet });
  assertEquals(started.length, 3);
  assertEquals(report.sent, 3);
  assert([...db.completions.values()].every((c) => c.ok && c.body?.startsWith("Vigil URGENT")));
});

Deno.test("retryable failures back off; permanent failures give up", async () => {
  const provider = new MockSmsProvider((to) => {
    if (to.endsWith("0102")) return new SmsError("twilio 503", true);
    if (to.endsWith("0103")) return new SmsError("twilio 400", false);
    return null;
  });
  const db = new FakeDb([[
    claimed(1, "+15555550101"),
    claimed(2, "+15555550102", { attempts: 2 }),
    claimed(3, "+15555550103"),
  ]]);
  const report = await dispatch(db, provider, { now, log: quiet });
  assertEquals(report, { reminders: 0, offline: 0, sent: 1, retrying: 1, failed: 1 });
  assertEquals(db.completions.get(2)!.retryAt!.toISOString(), "2026-11-20T05:43:00.000Z"); // +60 s
  assertEquals(db.completions.get(3)!.retryAt, null);
  assertEquals(provider.sent.map((m) => m.to), ["+15555550101"]);
});

Deno.test("a message that exhausted its attempts is not retried", async () => {
  const db = new FakeDb([[claimed(1, "+15555550101", { attempts: 5 })]]);
  const provider = new MockSmsProvider(() => new SmsError("timeout", true));
  const report = await dispatch(db, provider, { now, log: quiet });
  assertEquals(report.failed, 1);
  assertEquals(db.completions.get(1)!.retryAt, null);
});

Deno.test("inline dispatch skips periodic work", async () => {
  const db = new FakeDb([]);
  await dispatch(db, new MockSmsProvider(), { now, periodic: false });
  assertEquals(db.periodicCalls, 0);
  await dispatch(db, new MockSmsProvider(), { now });
  assertEquals(db.periodicCalls, 2);
});

Deno.test("backoff schedule", () => {
  assertEquals([1, 2, 3, 4, 5, 9].map(backoffSeconds), [30, 60, 120, 240, 480, 600]);
});
