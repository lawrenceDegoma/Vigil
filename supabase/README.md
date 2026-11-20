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
| `…0300_ingest_and_notify` | `private.*` engine: idempotent, reorder-tolerant ingestion; fan-out to every contact; reminder rounds; admin-only offline/recovery; per-hour cap (never on first urgent alerts); leased claims; pg_cron job calling the `dispatch` function |

Rules worth knowing:
- Devices never use Supabase user roles. Edge Functions authenticate the device token (stored only as a SHA-256 hash) and call `private.*` over a direct database connection.
- Reminders follow **acknowledgement**, not whether the episode ended. Any status other than `open` stops them.
- The pg_cron job does nothing until the Vault secrets `vigil_dispatch_url` and `vigil_dispatch_secret` exist (set per environment at deploy time).
- Test data uses fake `+1555555xxxx` numbers only.
