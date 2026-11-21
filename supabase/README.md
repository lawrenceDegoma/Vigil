# supabase — Vigil backend (Phase 3)

Local-first development with the Supabase CLI (pinned in the root `package.json`; needs Docker).

```bash
npm install                 # pinned supabase CLI + deno
npm run db:start            # local stack; applies migrations
npm run db:test             # pgTAP: permissions + ingestion/notification engine
npm run db:reset            # re-apply migrations from scratch
```

| Migration | Contents |
|---|---|
| `…0100_core_schema` | DESIGN §9 data model: households, memberships, contacts, devices, health, incidents, device events (idempotency log), incident actions, clips, notifications, audit log; `device_status_v` |
| `…0200_authorization` | RLS on every table, explicit grants (anon: nothing), admin-only columns, `incident_action` / `set_clip_preserved` RPCs, audit triggers |
| `…0400_clips` | private `incident-clips` bucket (no user storage policies), `clip_files` parts, `register_clip` / `complete_clip_part` (alerted incidents only), `clip_access`, cloud clip retention |
| `…0300_ingest_and_notify` | `private.*` engine: idempotent, reorder-tolerant ingestion; fan-out to every contact; reminder rounds; admin-only offline/recovery; per-hour cap (never on first urgent alerts); leased claims; pg_cron job calling the `dispatch` function |

Rules worth knowing:
- Devices never use Supabase user roles. Edge Functions authenticate the device token (stored only as a SHA-256 hash) and call `private.*` over a direct database connection.
- Reminders follow **acknowledgement**, not whether the episode ended. Any status other than `open` stops them.
- The pg_cron job does nothing until the Vault secrets `vigil_dispatch_url` and `vigil_dispatch_secret` exist (set per environment at deploy time).
- Test data uses fake `+1555555xxxx` numbers only.

Edge Functions (`supabase/functions/`):

| Function | Caller | Purpose |
|---|---|---|
| `device-ingest` | device token | incident events + heartbeats; inline alert dispatch |
| `dispatch` | pg_cron (shared secret) | reminders, admin-only offline notices, retries, health pruning, clip expiry |
| `clip-upload` | device token | register clip → signed upload URLs → complete (size-checked) |
| `clip-url` | signed-in user | 5-minute signed view links; download links for admins only; audited |

`make sms-dry-run` runs both end-to-end suites (alerts and clips) against the local stack with the mock SMS provider.

## Hosted deployment (done 2026-11-20, Free plan, mock SMS)

```bash
npx supabase login && npx supabase link --project-ref <ref>
npx supabase db push --linked
npx supabase functions deploy --project-ref <ref>
# secrets: generated locally into a 0600 temp env file, never printed, then deleted
npx supabase secrets set --project-ref <ref> --env-file <tmp>   # VIGIL_DISPATCH_SECRET, SMS_PROVIDER=mock
npx supabase db query --linked -f <tmp.sql>   # vault: vigil_dispatch_secret (same value), vigil_dispatch_url
```
Checked after deploying: all four functions return 401 without credentials; `dispatch` rejects a wrong secret; the `vigil-dispatch` cron job calls `dispatch` every minute and gets HTTP 200.

**Security advisor status.** `0014 extension_in_public` (pg_net) is fixed by migration 0500. Two warnings are accepted on purpose: `0029 authenticated_security_definer_function_executable` for `incident_action` and `set_clip_preserved`. Signed-in users must be able to call them (to acknowledge incidents, and for admins to preserve clips). Both need elevated rights to update tables users cannot write directly, and both re-check active household membership (and admin role for preserving) before acting. Covered by `tests/database/01_authorization.test.sql`.
