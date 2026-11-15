# Vigil contracts

Versioned JSON Schemas (draft 2020-12) for messages that cross a process or network boundary. They are the source of truth for edge (Python), backend (Supabase Edge Functions), and web (TypeScript) code.

| Schema | Producer → Consumer | Notes |
|---|---|---|
| `incident-event.v1` | edge → cloud ingestion | `event_id` is the idempotency key. `confidence` is an uncalibrated score. |
| `heartbeat.v1` | edge → cloud | Component-level health; the cloud derives `online/degraded/offline` from freshness and component flags. |

Rules:
- Breaking changes create a new version file (`*.v2.schema.json`); never edit a published version incompatibly.
- Messages never carry image data, raw frames, or phone numbers.
- Timestamps are RFC 3339 UTC.

The edge test suite validates generated messages against these schemas (`services/edge/tests/test_contracts.py`). TypeScript types will be generated from them in Phase 3.
