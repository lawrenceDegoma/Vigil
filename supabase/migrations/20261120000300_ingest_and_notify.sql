-- Device ingestion and notification engine (DESIGN §6, §8).
-- All functions live in `private` and are callable only by the database owner
-- (Edge Functions connect directly). Each takes `p_now` so tests can control time.

-- --------------------------------------------------------------- enqueueing
-- Queue one notification per distinct phone number in the household (or its
-- admins). Idempotent on `p_key`. Rate-capped except for round-1 urgent alerts.
create function private.enqueue(
  p_household uuid, p_kind public.notification_kind, p_round smallint, p_incident uuid,
  p_device uuid, p_key text, p_now timestamptz, p_admins_only boolean, p_uncapped boolean
) returns integer
language plpgsql security definer set search_path = '' as $$
declare
  v_cap integer;
  v_recent integer;
  v_status public.notification_status := 'pending';
  v_error text;
  v_n integer;
begin
  if not p_uncapped then
    select max_sms_per_hour into v_cap from public.household_settings
     where household_id = p_household;
    select count(*) into v_recent from public.notifications
     where household_id = p_household and created_at > p_now - interval '1 hour'
       and status <> 'cancelled';
    if v_recent >= coalesce(v_cap, 40) then
      v_status := 'cancelled';
      v_error := 'rate_limited';
    end if;
  end if;

  insert into public.notifications (household_id, incident_id, device_id, contact_id, to_e164,
                                    kind, round, dedup_key, status, not_before, last_error,
                                    created_at)
  select distinct on (c.phone_e164)
         p_household, p_incident, p_device, c.id, c.phone_e164, p_kind, p_round,
         p_key || ':' || c.phone_e164, v_status, p_now, v_error, p_now
    from public.notification_contacts c
    join public.memberships m on m.id = c.membership_id
   where m.household_id = p_household and m.active and c.enabled
     and (not p_admins_only or m.role = 'admin')
   order by c.phone_e164, m.role, c.created_at
  on conflict (dedup_key) do nothing;
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

-- ---------------------------------------------------------------- ingestion
create function private.authenticate_device(p_token_hash text)
returns table (id uuid, household_id uuid, device_key text)
language sql stable security definer set search_path = '' as $$
  select d.id, d.household_id, d.device_key from public.devices d
   where d.token_hash = p_token_hash and d.revoked_at is null
$$;

-- Apply one incident-event.v1 message. Idempotent on event_id; tolerant of
-- reordering (any event may create the incident; only newer events overwrite
-- mutable fields; severity and confidence only ratchet up).
create function private.ingest_incident_event(p_device uuid, e jsonb, p_now timestamptz)
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  d public.devices;
  s public.household_settings;
  inc public.incidents;
  v_incident uuid := (e ->> 'incident_id')::uuid;
  v_occurred timestamptz := (e ->> 'occurred_at')::timestamptz;
  v_severity public.severity := (e ->> 'severity')::public.severity;
  v_confidence real := (e ->> 'confidence')::real;
  v_ended timestamptz := (e ->> 'ended_at')::timestamptz;
  v_new boolean := false;
  v_queued integer := 0;
begin
  select * into d from public.devices where id = p_device and revoked_at is null;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown or revoked device');
  end if;
  if e ->> 'device_id' is distinct from d.device_key then
    return jsonb_build_object('status', 'rejected', 'reason', 'device_id mismatch');
  end if;

  select * into inc from public.incidents where id = v_incident for update;
  if found and inc.device_id <> d.id then
    return jsonb_build_object('status', 'rejected', 'reason', 'incident belongs to another device');
  end if;

  insert into public.device_events (event_id, device_id, incident_id, event_type, occurred_at,
                                    received_at, payload)
  values ((e ->> 'event_id')::uuid, d.id, v_incident, e ->> 'event_type', v_occurred, p_now, e)
  on conflict (event_id) do nothing;
  if not found then
    return jsonb_build_object('status', 'duplicate', 'incident_id', v_incident);
  end if;

  if inc.id is null then
    insert into public.incidents (id, household_id, device_id, signal_type, severity, started_at,
                                  ended_at, confidence, rule_version, follows_incident_id,
                                  features, last_event_at, first_received_at, created_at,
                                  updated_at)
    values (v_incident, d.household_id, d.id, (e ->> 'signal_type')::public.signal_type,
            v_severity, (e ->> 'started_at')::timestamptz, v_ended, v_confidence,
            e ->> 'rule_version', (e ->> 'follows_incident_id')::uuid,
            coalesce(e -> 'features', '{}'::jsonb), v_occurred, p_now, p_now, p_now)
    returning * into inc;
    v_new := true;
  elsif v_occurred >= inc.last_event_at then
    -- Newest view wins for ended_at (an incident can resume after an `ended`).
    update public.incidents
       set severity = greatest(severity, v_severity),
           confidence = greatest(confidence, v_confidence),
           ended_at = v_ended,
           features = coalesce(e -> 'features', features),
           last_event_at = v_occurred,
           updated_at = p_now
     where id = v_incident
    returning * into inc;
  else
    update public.incidents
       set severity = greatest(severity, v_severity),
           confidence = greatest(confidence, v_confidence),
           updated_at = p_now
     where id = v_incident
    returning * into inc;
  end if;

  -- Alert once per severity level reached (an escalation alerts again).
  select * into s from public.household_settings where household_id = d.household_id;
  if (inc.alerted_severity is null or inc.alerted_severity < inc.severity)
     and (inc.severity = 'urgent' or coalesce(s.warning_alerts_enabled, true)) then
    v_queued := private.enqueue(d.household_id, 'incident_alert', 1::smallint, inc.id, d.id,
                                'alert:' || inc.id || ':' || inc.severity, p_now, false,
                                inc.severity = 'urgent');
    update public.incidents set alerted_at = p_now, alerted_severity = inc.severity
     where id = inc.id;
  end if;

  return jsonb_build_object('status', 'accepted', 'incident_id', inc.id, 'new', v_new,
                            'notifications', v_queued);
end;
$$;

-- Apply one heartbeat.v1 message. Freshness uses the cloud receive time
-- (ADR-016); a device coming back from offline gets an admin-only recovery notice.
create function private.ingest_heartbeat(p_device uuid, h jsonb, p_now timestamptz)
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  d public.devices;
  s public.household_settings;
  v_camera boolean := (h -> 'camera' ->> 'ok')::boolean;
  v_detector boolean := (h -> 'detector' ->> 'ok')::boolean;
  v_maint boolean := (h ->> 'maintenance')::boolean;
  v_status public.device_status;
  v_queued integer := 0;
begin
  select * into d from public.devices where id = p_device and revoked_at is null for update;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown or revoked device');
  end if;
  if h ->> 'device_id' is distinct from d.device_key then
    return jsonb_build_object('status', 'rejected', 'reason', 'device_id mismatch');
  end if;

  -- Replayed old heartbeats from the device queue must not make a device look fresh
  -- when it is not; only the newest heartbeat drives status.
  insert into public.device_health (device_id, received_at, sent_at, camera_ok, detector_ok,
                                    maintenance, fps, disk_free_bytes, queue_depth,
                                    temperature_c, payload)
  values (d.id, p_now, (h ->> 'sent_at')::timestamptz, v_camera, v_detector, v_maint,
          (h -> 'camera' ->> 'fps')::real, (h -> 'storage' ->> 'free_bytes')::bigint,
          (h ->> 'queue_depth')::integer, (h ->> 'temperature_c')::real, h);

  v_status := case
    when v_maint then 'maintenance'
    when v_camera and v_detector then 'online'
    else 'degraded'
  end;

  if d.offline_since is not null then
    select * into s from public.household_settings where household_id = d.household_id;
    if coalesce(s.notify_recovery, true) then
      v_queued := private.enqueue(d.household_id, 'device_recovered', 1::smallint, null, d.id,
                                  'recovered:' || d.id || ':' || extract(epoch from d.offline_since),
                                  p_now, true, false);
    end if;
  end if;

  update public.devices
     set last_heartbeat_at = p_now, last_status = v_status, offline_since = null,
         software_version = coalesce(h ->> 'software_version', software_version)
   where id = d.id;

  return jsonb_build_object('status', 'accepted', 'device_status', v_status,
                            'notifications', v_queued);
end;
$$;

-- ------------------------------------------------------------ periodic work
-- Reminder rounds for urgent incidents still `open` (nobody acknowledged).
create function private.schedule_reminders(p_now timestamptz) returns integer
language plpgsql security definer set search_path = '' as $$
declare
  r record;
  v_round integer;
  v_total integer := 0;
begin
  for r in
    select i.id, i.household_id, i.device_id, i.alerted_at, s.reminder_rounds,
           s.reminder_interval_seconds
      from public.incidents i
      join public.household_settings s on s.household_id = i.household_id
     where i.status = 'open' and i.alerted_severity = 'urgent' and i.alerted_at is not null
       and i.alerted_at > p_now - interval '6 hours'
  loop
    for v_round in 1 .. r.reminder_rounds loop
      exit when p_now < r.alerted_at + make_interval(secs => v_round * r.reminder_interval_seconds);
      v_total := v_total + private.enqueue(
        r.household_id, 'incident_reminder', (v_round + 1)::smallint, r.id, r.device_id,
        'reminder:' || r.id || ':' || (v_round + 1), p_now, false, false);
    end loop;
  end loop;
  return v_total;
end;
$$;

-- Admin-only offline notice when heartbeats go stale (never during maintenance).
create function private.check_stale_devices(p_now timestamptz) returns integer
language plpgsql security definer set search_path = '' as $$
declare
  r record;
  v_total integer := 0;
begin
  for r in
    select d.id, d.household_id, d.last_heartbeat_at
      from public.devices d
      join public.household_settings s on s.household_id = d.household_id
     where d.revoked_at is null and d.offline_since is null and d.last_heartbeat_at is not null
       and d.last_status <> 'maintenance'
       and d.last_heartbeat_at < p_now - make_interval(secs => s.offline_grace_seconds)
     for update of d
  loop
    update public.devices set offline_since = r.last_heartbeat_at where id = r.id;
    v_total := v_total + private.enqueue(
      r.household_id, 'device_offline', 1::smallint, null, r.id,
      'offline:' || r.id || ':' || extract(epoch from r.last_heartbeat_at), p_now, true, false);
  end loop;
  return v_total;
end;
$$;

-- --------------------------------------------------------------- dispatching
-- Claim due notifications with a lease (at-least-once delivery). Cancels
-- reminders whose incident is no longer open and messages to disabled contacts,
-- and fails messages that exhausted their attempts.
create function private.claim_notifications(p_now timestamptz, p_limit integer,
                                            p_lease_seconds integer default 120)
returns table (
  id bigint, kind public.notification_kind, round smallint, to_e164 text, attempts smallint,
  incident_id uuid, signal_type public.signal_type, severity public.severity,
  started_at timestamptz, incident_status public.incident_status, device_name text,
  subject_name text, timezone text, reminder_rounds smallint, offline_since timestamptz,
  last_heartbeat_at timestamptz
)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
begin
  update public.notifications n
     set status = 'cancelled', last_error = 'incident no longer open'
    from public.incidents i
   where n.incident_id = i.id and n.kind = 'incident_reminder'
     and n.status in ('pending', 'sending') and i.status <> 'open' and n.not_before <= p_now;

  update public.notifications n
     set status = 'cancelled', last_error = 'contact disabled'
    from public.notification_contacts c
   where n.contact_id = c.id and not c.enabled and n.status = 'pending';

  update public.notifications n
     set status = 'failed', last_error = coalesce(n.last_error, 'max attempts')
   where n.status in ('pending', 'sending') and n.attempts >= 5 and n.not_before <= p_now;

  return query
  with due as (
    select n.id from public.notifications n
     where n.status in ('pending', 'sending') and n.not_before <= p_now
     order by n.not_before, n.id
     limit p_limit
     for update skip locked
  ), claimed as (
    update public.notifications n
       set status = 'sending', attempts = n.attempts + 1,
           not_before = p_now + make_interval(secs => p_lease_seconds)
      from due where n.id = due.id
    returning n.*
  )
  select c.id, c.kind, c.round, c.to_e164, c.attempts, i.id, i.signal_type, i.severity,
         i.started_at, i.status, d.name, d.subject_name, s.timezone, s.reminder_rounds,
         d.offline_since, d.last_heartbeat_at
    from claimed c
    left join public.incidents i on i.id = c.incident_id
    left join public.devices d on d.id = coalesce(c.device_id, i.device_id)
    join public.household_settings s on s.household_id = c.household_id
   order by c.id;
end;
$$;

create function private.complete_notification(
  p_id bigint, p_ok boolean, p_provider text, p_provider_message_id text, p_body text,
  p_error text, p_retry_at timestamptz, p_now timestamptz
) returns public.notification_status
language plpgsql security definer set search_path = '' as $$
declare
  v_status public.notification_status;
begin
  update public.notifications n
     set status = case
           when p_ok then 'sent'
           when p_retry_at is not null and n.attempts < 5 then 'pending'
           else 'failed' end::public.notification_status,
         sent_at = case when p_ok then p_now end,
         not_before = coalesce(p_retry_at, n.not_before),
         body = coalesce(p_body, n.body),
         provider = p_provider,
         provider_message_id = coalesce(p_provider_message_id, n.provider_message_id),
         last_error = case when p_ok then null else left(p_error, 300) end
   where n.id = p_id and n.status = 'sending'
  returning n.status into v_status;
  return v_status;
end;
$$;

-- Bound the health history (dashboards need recent data only).
create function private.prune_device_health(p_now timestamptz, p_keep interval default '14 days')
returns integer
language plpgsql security definer set search_path = '' as $$
declare
  v_n integer;
begin
  delete from public.device_health where received_at < p_now - p_keep;
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

revoke all on all functions in schema private from public, anon, authenticated;
grant execute on function private.is_member(uuid), private.is_admin(uuid),
  private.own_membership(uuid), private.membership_household(uuid),
  private.valid_timezone(text) to authenticated;

-- ------------------------------------------------------------- scheduling
-- Every minute, call the `dispatch` Edge Function (reminders, stale checks,
-- retries). URL and shared secret come from Vault per environment; the job does
-- nothing until both are set (see supabase/README.md).
create extension if not exists pg_net;
create extension if not exists pg_cron;

select cron.schedule(
  'vigil-dispatch', '* * * * *',
  $job$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'vigil_dispatch_url'),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-vigil-dispatch-secret',
      (select decrypted_secret from vault.decrypted_secrets where name = 'vigil_dispatch_secret')),
    body := '{}'::jsonb,
    timeout_milliseconds := 20000)
  where exists (select 1 from vault.decrypted_secrets where name = 'vigil_dispatch_url')
    and exists (select 1 from vault.decrypted_secrets where name = 'vigil_dispatch_secret');
  $job$
);
