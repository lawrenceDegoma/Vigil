-- Vigil core schema (DESIGN §9, Phase 3).
-- Households own devices, members, contacts and incidents. Every table has RLS
-- (next migration). Device ingestion and notification work run server-side via
-- functions in the non-exposed `private` schema; nothing here is writable by
-- the anon role, and family members change incidents only through RPCs.

create schema if not exists private;

create type public.member_role as enum ('admin', 'family');
create type public.signal_type as enum ('convulsion', 'vomiting', 'fall', 'prolonged_inactivity');
create type public.severity as enum ('warning', 'urgent');  -- order matters: warning < urgent
create type public.incident_status as enum (
  'open', 'acknowledged', 'responding', 'checked', 'resolved', 'false_positive', 'needs_review'
);
create type public.device_status as enum ('online', 'degraded', 'offline', 'maintenance', 'unknown');
create type public.notification_kind as enum (
  'incident_alert', 'incident_reminder', 'device_offline', 'device_recovered'
);
create type public.notification_status as enum ('pending', 'sending', 'sent', 'failed', 'cancelled');

create table public.households (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(name) between 1 and 100),
  created_at timestamptz not null default now()
);

create table public.household_settings (
  household_id uuid primary key references public.households on delete cascade,
  timezone text not null default 'UTC',
  warning_alerts_enabled boolean not null default true,
  -- "one or two SMS reminder rounds" (CLAUDE.md fixed requirements)
  reminder_rounds smallint not null default 2 check (reminder_rounds between 1 and 2),
  reminder_interval_seconds integer not null default 300
    check (reminder_interval_seconds between 60 and 3600),
  offline_grace_seconds integer not null default 300
    check (offline_grace_seconds between 120 and 86400),
  notify_recovery boolean not null default true,
  -- Caps reminders, warnings and device notices; round-1 urgent alerts are never capped.
  max_sms_per_hour integer not null default 40 check (max_sms_per_hour between 5 and 500),
  retention_days integer not null default 30 check (retention_days between 1 and 365),
  updated_at timestamptz not null default now()
);

create function private.valid_timezone(tz text) returns boolean
language sql stable set search_path = '' as $$
  select exists (select 1 from pg_catalog.pg_timezone_names where name = tz)
$$;
alter table public.household_settings
  add constraint household_settings_timezone_valid check (private.valid_timezone(timezone));

create table public.memberships (
  id uuid primary key default gen_random_uuid(),
  household_id uuid not null references public.households on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  role public.member_role not null,
  display_name text not null check (length(display_name) between 1 and 80),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (household_id, user_id)
);
create index memberships_user_idx on public.memberships (user_id) where active;

create table public.notification_contacts (
  id uuid primary key default gen_random_uuid(),
  membership_id uuid not null references public.memberships on delete cascade,
  phone_e164 text not null check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  enabled boolean not null default true,
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  unique (membership_id, phone_e164)
);

create table public.devices (
  id uuid primary key default gen_random_uuid(),
  household_id uuid not null references public.households on delete cascade,
  device_key text not null check (device_key ~ '^[A-Za-z0-9._-]{1,64}$'),  -- VIGIL_DEVICE_ID
  name text not null check (length(name) between 1 and 80),
  subject_name text not null default 'the subject' check (length(subject_name) between 1 and 60),
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),  -- sha256 of device token
  upload_clips boolean not null default true,  -- ADR-010 (b); false = clips stay on the device
  last_heartbeat_at timestamptz,  -- cloud receive time (ADR-016)
  last_status public.device_status not null default 'unknown',  -- from the last heartbeat
  offline_since timestamptz,  -- set by the stale-heartbeat checker
  software_version text,
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  unique (household_id, device_key)
);

create table public.device_health (
  id bigint generated always as identity primary key,
  device_id uuid not null references public.devices on delete cascade,
  received_at timestamptz not null,
  sent_at timestamptz,  -- device clock; may be wrong before NTP sync (ADR-016)
  camera_ok boolean not null,
  detector_ok boolean not null,
  maintenance boolean not null,
  fps real,
  disk_free_bytes bigint,
  queue_depth integer,
  temperature_c real,
  payload jsonb not null
);
create index device_health_device_time_idx on public.device_health (device_id, received_at desc);

create table public.incidents (
  id uuid primary key,  -- generated on the device
  household_id uuid not null references public.households on delete cascade,
  device_id uuid not null references public.devices on delete cascade,
  signal_type public.signal_type not null,
  severity public.severity not null,
  status public.incident_status not null default 'open',
  started_at timestamptz not null,
  ended_at timestamptz,
  confidence real not null check (confidence between 0 and 1),  -- uncalibrated score
  rule_version text not null,
  follows_incident_id uuid,  -- repeat episode / escalation (no FK: may arrive out of order)
  features jsonb not null default '{}'::jsonb,
  last_event_at timestamptz not null,  -- occurred_at of the newest applied device event
  first_received_at timestamptz not null,
  alerted_at timestamptz,
  alerted_severity public.severity,
  status_changed_at timestamptz,
  status_changed_by uuid references auth.users on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index incidents_household_time_idx on public.incidents (household_id, started_at desc);
create index incidents_open_idx on public.incidents (household_id) where status = 'open';

-- Raw device events: the idempotency log for ingestion (event_id from the device).
create table public.device_events (
  event_id uuid primary key,
  device_id uuid not null references public.devices on delete cascade,
  incident_id uuid not null,
  event_type text not null check (event_type in ('incident_opened', 'incident_updated', 'incident_ended')),
  occurred_at timestamptz not null,
  received_at timestamptz not null,
  payload jsonb not null
);
create index device_events_incident_idx on public.device_events (incident_id);

-- Human actions on incidents (DESIGN §9 `incident_events`, renamed to avoid
-- confusion with device events).
create table public.incident_actions (
  id bigint generated always as identity primary key,
  incident_id uuid not null references public.incidents on delete cascade,
  household_id uuid not null references public.households on delete cascade,
  actor_id uuid references auth.users on delete set null,
  action text not null check (action in (
    'acknowledged', 'responding', 'checked', 'resolved', 'false_positive', 'needs_review', 'note'
  )),
  note text check (length(note) <= 1000),
  created_at timestamptz not null default now()
);
create index incident_actions_incident_idx on public.incident_actions (incident_id, created_at);

create table public.clips (
  id uuid primary key,  -- generated on the device
  household_id uuid not null references public.households on delete cascade,
  device_id uuid not null references public.devices on delete cascade,
  storage_path text unique,  -- null until uploaded (ADR-010)
  started_at timestamptz not null,
  ended_at timestamptz not null,
  bytes bigint not null check (bytes >= 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  uploaded_at timestamptz,
  expires_at timestamptz,
  preserved_at timestamptz,
  preserved_by uuid references auth.users on delete set null,
  created_at timestamptz not null default now()
);

create table public.clip_incidents (
  clip_id uuid not null references public.clips on delete cascade,
  incident_id uuid not null references public.incidents on delete cascade,
  primary key (clip_id, incident_id)
);

create table public.notifications (
  id bigint generated always as identity primary key,
  household_id uuid not null references public.households on delete cascade,
  incident_id uuid references public.incidents on delete cascade,
  device_id uuid references public.devices on delete cascade,
  contact_id uuid references public.notification_contacts on delete set null,
  to_e164 text not null,  -- snapshot at enqueue time
  kind public.notification_kind not null,
  round smallint not null default 1 check (round between 1 and 3),
  dedup_key text not null unique,
  status public.notification_status not null default 'pending',
  not_before timestamptz not null,  -- send time, or lease expiry while 'sending'
  attempts smallint not null default 0,
  body text check (length(body) <= 480),  -- rendered text, stored when sent (audit)
  provider text,
  provider_message_id text,
  last_error text,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index notifications_due_idx on public.notifications (not_before)
  where status in ('pending', 'sending');
create index notifications_household_time_idx on public.notifications (household_id, created_at desc);

create table public.audit_log (
  id bigint generated always as identity primary key,
  household_id uuid references public.households on delete cascade,
  actor_id uuid references auth.users on delete set null,
  action text not null,
  target_type text,
  target_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index audit_log_household_time_idx on public.audit_log (household_id, created_at desc);

-- Honest dashboard status from fresh heartbeats (DESIGN §8), computed at read time
-- so it never depends on the checker having run.
create view public.device_status_v with (security_invoker = true) as
select
  d.id,
  d.household_id,
  d.device_key,
  d.name,
  d.subject_name,
  d.software_version,
  d.last_heartbeat_at,
  d.upload_clips,
  case
    when d.revoked_at is not null then 'offline'
    when d.last_heartbeat_at is null then 'unknown'
    when d.last_heartbeat_at < now() - make_interval(secs => s.offline_grace_seconds) then 'offline'
    else d.last_status
  end::public.device_status as status
from public.devices d
join public.household_settings s on s.household_id = d.household_id;
