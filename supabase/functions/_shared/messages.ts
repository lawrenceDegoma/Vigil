// SMS wording (DESIGN §6). Rules enforced by messages_test.ts:
//  * observations only ("possible ..."), never a diagnosis or treatment advice;
//  * warnings never say a seizure is imminent;
//  * GSM-7 friendly ASCII where we control the text (non-GSM characters force
//    UCS-2 and shrink a segment from 160 to 70 characters);
//  * no phone numbers or video in the body; at most one link.

import type { SignalType } from "./contracts.ts";

export type NotificationKind =
  | "incident_alert"
  | "incident_reminder"
  | "device_offline"
  | "device_recovered"
  | "test_alert";

export interface MessageContext {
  kind: NotificationKind;
  round: number; // 1 = first alert; reminders are 2..3
  reminderRounds: number; // configured reminder rounds (1-2)
  signalType: SignalType | null;
  severity: "warning" | "urgent" | null;
  incidentId: string | null;
  startedAt: Date | null;
  deviceName: string | null;
  subjectName: string | null;
  timezone: string;
  offlineSince: Date | null;
  now: Date;
  appUrl?: string | null;
}

const OBSERVATION: Record<SignalType, string> = {
  convulsion: "possible seizure-like movement",
  vomiting: "possible vomiting",
  fall: "a possible fall",
  prolonged_inactivity: "unusually long inactivity",
};

const LATE_AFTER_MS = 10 * 60 * 1000;

/** Replace characters outside plain ASCII that ICU or user input may introduce. */
export function plain(text: string): string {
  return text
    .replace(/[   ]/g, " ") // ICU puts a narrow no-break space before AM/PM
    .replace(/[‘’]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/[–—]/g, "-")
    .replace(/\s+/g, " ")
    .trim();
}

function name(value: string | null, fallback: string, max = 40): string {
  const v = plain(value ?? "");
  return (v || fallback).slice(0, max);
}

/** "9:41 PM", or "Nov 19, 9:41 PM" when not the same local day as `now`. */
export function localTime(at: Date, now: Date, timezone: string): string {
  const day = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const sameDay = day.format(at) === day.format(now);
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    ...(sameDay ? {} : { month: "short", day: "numeric" }),
  });
  return plain(fmt.format(at));
}

function link(ctx: MessageContext): string {
  if (!ctx.appUrl || !ctx.incidentId) return "";
  return ` ${ctx.appUrl.replace(/\/+$/, "")}/incidents/${ctx.incidentId}`;
}

export function renderMessage(ctx: MessageContext): string {
  const device = name(ctx.deviceName, "your Vigil camera");
  switch (ctx.kind) {
    case "incident_alert":
    case "incident_reminder": {
      if (!ctx.signalType || !ctx.startedAt) throw new Error("incident context missing");
      const subject = name(ctx.subjectName, "your pet");
      const what = OBSERVATION[ctx.signalType];
      let when = localTime(ctx.startedAt, ctx.now, ctx.timezone);
      if (ctx.now.getTime() - ctx.startedAt.getTime() > LATE_AFTER_MS) when += " (reported late)";
      if (ctx.kind === "incident_reminder") {
        const n = Math.max(1, ctx.round - 1);
        return plain(
          `Vigil reminder ${n}/${ctx.reminderRounds}: ${what} for ${subject} at ${when} ` +
            `has not been acknowledged.${link(ctx)}`,
        );
      }
      const urgent = ctx.severity === "urgent";
      return plain(
        `Vigil${urgent ? " URGENT" : ""}: ${what} seen for ${subject} at ${when} (${device}).` +
          `${urgent ? " Please check now." : ""}${link(ctx)}`,
      );
    }
    case "device_offline": {
      const since = ctx.offlineSince
        ? ` No heartbeat since ${localTime(ctx.offlineSince, ctx.now, ctx.timezone)}.`
        : "";
      return plain(
        `Vigil: ${device} is OFFLINE.${since} Monitoring and alerts may not be working.`,
      );
    }
    case "test_alert": {
      const at = localTime(ctx.now, ctx.now, ctx.timezone);
      const from = ctx.deviceName ? ` from ${device}` : "";
      return plain(
        `Vigil TEST${from} at ${at}: this is only a test of your alerts. No action needed. ` +
          "Reply STOP to opt out.",
      );
    }
    case "device_recovered": {
      const since = ctx.offlineSince
        ? ` (offline since ${localTime(ctx.offlineSince, ctx.now, ctx.timezone)})`
        : "";
      return plain(`Vigil: ${device} is back online${since}.`);
    }
  }
}

/** Mask a phone number for logs: +1555***0102. Logs never carry full numbers. */
export function maskPhone(e164: string): string {
  return e164.length > 8 ? `${e164.slice(0, 5)}***${e164.slice(-4)}` : "***";
}
