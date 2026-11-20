// SMS provider abstraction (DESIGN §10). `mock` is the default everywhere.
// Real delivery (Twilio) requires SMS_LIVE_APPROVED=yes in addition to
// credentials: the owner must explicitly approve sending real messages.

import { maskPhone } from "./messages.ts";

export interface SendResult {
  id: string;
}

export interface SmsProvider {
  readonly name: string;
  send(to: string, body: string): Promise<SendResult>;
}

/** `retryable` = worth trying again later (rate limit, outage, timeout). */
export class SmsError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export interface MockMessage {
  to: string;
  body: string;
  id: string;
}

/** Records messages instead of sending them. Optional failure injection for tests. */
export class MockSmsProvider implements SmsProvider {
  readonly name = "mock";
  readonly sent: MockMessage[] = [];
  private seq = 0;

  constructor(
    private readonly fail?: (to: string, attempt: number) => SmsError | null,
    private readonly log: (line: string) => void = () => {},
  ) {}

  private attempts = new Map<string, number>();

  send(to: string, body: string): Promise<SendResult> {
    const attempt = (this.attempts.get(to) ?? 0) + 1;
    this.attempts.set(to, attempt);
    const err = this.fail?.(to, attempt);
    if (err) return Promise.reject(err);
    const id = `mock-${++this.seq}`;
    this.sent.push({ to, body, id });
    this.log(`mock sms ${id} to ${maskPhone(to)} (${body.length} chars)`);
    return Promise.resolve({ id });
  }
}

export interface TwilioOptions {
  accountSid: string;
  authToken: string;
  from?: string; // E.164 sender, or
  messagingServiceSid?: string; // a Messaging Service (recommended for US A2P)
  fetch?: typeof fetch;
  timeoutMs?: number;
}

export class TwilioSmsProvider implements SmsProvider {
  readonly name = "twilio";
  private readonly fetch: typeof fetch;

  constructor(private readonly opts: TwilioOptions) {
    if (!/^AC[0-9a-f]{32}$/i.test(opts.accountSid)) throw new Error("invalid Twilio account SID");
    if (!opts.authToken) throw new Error("missing Twilio auth token");
    if (!opts.from && !opts.messagingServiceSid) {
      throw new Error("Twilio needs a sender number or messaging service");
    }
    this.fetch = opts.fetch ?? fetch;
  }

  async send(to: string, body: string): Promise<SendResult> {
    const form = new URLSearchParams({ To: to, Body: body });
    if (this.opts.messagingServiceSid) {
      form.set("MessagingServiceSid", this.opts.messagingServiceSid);
    } else form.set("From", this.opts.from!);
    const url = `https://api.twilio.com/2010-04-01/Accounts/${this.opts.accountSid}/Messages.json`;
    let res: Response;
    try {
      res = await this.fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Basic ${btoa(`${this.opts.accountSid}:${this.opts.authToken}`)}`,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: form,
        signal: AbortSignal.timeout(this.opts.timeoutMs ?? 10_000),
      });
    } catch (e) {
      throw new SmsError(`twilio request failed: ${(e as Error).name}`, true);
    }
    const data = await res.json().catch(() => ({})) as { sid?: string; code?: number };
    if (res.ok && data.sid) return { id: data.sid };
    // 429 and 5xx are transient; other 4xx (invalid number, unverified sender) are not.
    const retryable = res.status === 429 || res.status >= 500;
    throw new SmsError(`twilio ${res.status}${data.code ? ` code ${data.code}` : ""}`, retryable);
  }
}

export function providerFromEnv(
  env: (key: string) => string | undefined,
  log: (line: string) => void = console.log,
): SmsProvider {
  const kind = (env("SMS_PROVIDER") ?? "mock").toLowerCase();
  if (kind === "mock") return new MockSmsProvider(undefined, log);
  if (kind === "twilio") {
    if (env("SMS_LIVE_APPROVED") !== "yes") {
      throw new Error("SMS_PROVIDER=twilio requires SMS_LIVE_APPROVED=yes (owner approval)");
    }
    return new TwilioSmsProvider({
      accountSid: env("TWILIO_ACCOUNT_SID") ?? "",
      authToken: env("TWILIO_AUTH_TOKEN") ?? "",
      from: env("TWILIO_FROM") || undefined,
      messagingServiceSid: env("TWILIO_MESSAGING_SERVICE_SID") || undefined,
    });
  }
  throw new Error(`unknown SMS_PROVIDER ${kind}`);
}
