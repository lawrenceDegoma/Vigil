// Notification dispatch: periodic checks, then claim -> render -> send -> record.
// Pure orchestration over the EngineDb interface so it is testable without a
// database. Delivery is at-least-once: a crash after sending but before
// recording means the lease expires and the message is retried.

import { maskPhone, type NotificationKind, renderMessage } from "./messages.ts";
import type { SignalType } from "./contracts.ts";
import { SmsError, type SmsProvider } from "./sms.ts";

export interface ClaimedNotification {
  id: number;
  kind: NotificationKind;
  round: number;
  to_e164: string;
  attempts: number;
  incident_id: string | null;
  signal_type: SignalType | null;
  severity: "warning" | "urgent" | null;
  started_at: Date | null;
  device_name: string | null;
  subject_name: string | null;
  timezone: string;
  reminder_rounds: number;
  offline_since: Date | null;
}

export interface Completion {
  ok: boolean;
  provider: string;
  providerMessageId: string | null;
  body: string | null;
  error: string | null;
  retryAt: Date | null;
}

export interface EngineDb {
  scheduleReminders(now: Date): Promise<number>;
  checkStaleDevices(now: Date): Promise<number>;
  claim(now: Date, limit: number, leaseSeconds: number): Promise<ClaimedNotification[]>;
  complete(id: number, result: Completion, now: Date): Promise<void>;
}

export interface DispatchOptions {
  now?: () => Date;
  appUrl?: string | null;
  batchSize?: number;
  deadlineMs?: number;
  periodic?: boolean; // run reminders + stale checks first (cron); false for inline dispatch
  log?: (line: string) => void;
}

export interface DispatchReport {
  reminders: number;
  offline: number;
  sent: number;
  retrying: number;
  failed: number;
}

/** 30 s, 60 s, 120 s, 240 s ... capped at 10 min. */
export function backoffSeconds(attempts: number): number {
  return Math.min(600, 30 * 2 ** Math.max(0, attempts - 1));
}

export async function dispatch(
  db: EngineDb,
  provider: SmsProvider,
  opts: DispatchOptions = {},
): Promise<DispatchReport> {
  const now = opts.now ?? (() => new Date());
  const log = opts.log ?? console.log;
  const batchSize = opts.batchSize ?? 50;
  const deadline = Date.now() + (opts.deadlineMs ?? 20_000);
  const report: DispatchReport = { reminders: 0, offline: 0, sent: 0, retrying: 0, failed: 0 };

  if (opts.periodic !== false) {
    report.reminders = await db.scheduleReminders(now());
    report.offline = await db.checkStaleDevices(now());
  }

  while (Date.now() < deadline) {
    const batch = await db.claim(now(), batchSize, 120);
    if (batch.length === 0) break;
    // Concurrent fan-out: every recipient in the batch is sent to at once.
    const outcomes = await Promise.allSettled(batch.map(async (n) => {
      const body = renderMessage({
        kind: n.kind,
        round: n.round,
        reminderRounds: n.reminder_rounds,
        signalType: n.signal_type,
        severity: n.severity,
        incidentId: n.incident_id,
        startedAt: n.started_at,
        deviceName: n.device_name,
        subjectName: n.subject_name,
        timezone: n.timezone,
        offlineSince: n.offline_since,
        now: now(),
        appUrl: opts.appUrl,
      });
      const res = await provider.send(n.to_e164, body);
      return { body, id: res.id };
    }));
    for (const [i, outcome] of outcomes.entries()) {
      const n = batch[i];
      if (outcome.status === "fulfilled") {
        await db.complete(n.id, {
          ok: true,
          provider: provider.name,
          providerMessageId: outcome.value.id,
          body: outcome.value.body,
          error: null,
          retryAt: null,
        }, now());
        report.sent++;
        continue;
      }
      const err = outcome.reason;
      const retryable = err instanceof SmsError ? err.retryable : false;
      const message = err instanceof Error ? err.message : String(err);
      const retryAt = retryable && n.attempts < 5
        ? new Date(now().getTime() + backoffSeconds(n.attempts) * 1000)
        : null;
      await db.complete(n.id, {
        ok: false,
        provider: provider.name,
        providerMessageId: null,
        body: null,
        error: message,
        retryAt,
      }, now());
      if (retryAt) report.retrying++;
      else report.failed++;
      log(
        `notification ${n.id} (${n.kind}) to ${maskPhone(n.to_e164)} failed: ${message}` +
          (retryAt ? `; retry at ${retryAt.toISOString()}` : "; giving up"),
      );
    }
  }
  return report;
}
