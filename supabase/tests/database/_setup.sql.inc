-- Shared fixture (included verbatim at the top of each test file by scripts/gen-db-tests.py).
-- Two households, fake 555 numbers only. Times are fixed so tests control the clock.
create extension if not exists pgtap with schema extensions;

insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'admin1@example.test'),
  ('00000000-0000-0000-0000-0000000000f1', 'fam1@example.test'),
  ('00000000-0000-0000-0000-0000000000f2', 'fam2@example.test'),
  ('00000000-0000-0000-0000-0000000000a2', 'admin2@example.test'),
  ('00000000-0000-0000-0000-0000000000ee', 'outsider@example.test');

insert into public.households (id, name) values
  ('11111111-0000-0000-0000-000000000001', 'Household One'),
  ('11111111-0000-0000-0000-000000000002', 'Household Two');
insert into public.household_settings (household_id, timezone) values
  ('11111111-0000-0000-0000-000000000001', 'America/Los_Angeles'),
  ('11111111-0000-0000-0000-000000000002', 'UTC');

insert into public.memberships (id, household_id, user_id, role, display_name) values
  ('22222222-0000-0000-0000-0000000000a1', '11111111-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a1', 'admin', 'Admin One'),
  ('22222222-0000-0000-0000-0000000000f1', '11111111-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000f1', 'family', 'Family One'),
  ('22222222-0000-0000-0000-0000000000f2', '11111111-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000f2', 'family', 'Family Two'),
  ('22222222-0000-0000-0000-0000000000a2', '11111111-0000-0000-0000-000000000002', '00000000-0000-0000-0000-0000000000a2', 'admin', 'Admin Two');

insert into public.notification_contacts (id, membership_id, phone_e164) values
  ('33333333-0000-0000-0000-0000000000a1', '22222222-0000-0000-0000-0000000000a1', '+15555550101'),
  ('33333333-0000-0000-0000-0000000000f1', '22222222-0000-0000-0000-0000000000f1', '+15555550102'),
  ('33333333-0000-0000-0000-0000000000f2', '22222222-0000-0000-0000-0000000000f2', '+15555550103'),
  ('33333333-0000-0000-0000-0000000000a2', '22222222-0000-0000-0000-0000000000a2', '+15555550201');

insert into public.devices (id, household_id, device_key, name, subject_name, token_hash) values
  ('44444444-0000-0000-0000-000000000001', '11111111-0000-0000-0000-000000000001', 'pi-livingroom', 'Living room', 'Rex', encode(extensions.digest('tok-1', 'sha256'), 'hex')),
  ('44444444-0000-0000-0000-000000000002', '11111111-0000-0000-0000-000000000002', 'pi-livingroom', 'Lounge', 'Nan', encode(extensions.digest('tok-2', 'sha256'), 'hex'));

-- incident-event.v1 builder
create function pg_temp.ev(
  p_event uuid, p_incident uuid, p_type text, p_occurred timestamptz,
  p_signal text default 'convulsion', p_severity text default 'urgent',
  p_ended timestamptz default null, p_device text default 'pi-livingroom',
  p_confidence real default 0.8
) returns jsonb language sql as $$
  select jsonb_build_object(
    'schema_version', 'incident-event.v1', 'event_id', p_event, 'device_id', p_device,
    'incident_id', p_incident, 'event_type', p_type, 'signal_type', p_signal,
    'severity', p_severity, 'occurred_at', p_occurred, 'started_at', p_occurred - interval '10 seconds',
    'ended_at', p_ended, 'confidence', p_confidence, 'rule_version', 'motion-rules-0.1.0+test',
    'features', jsonb_build_object('peak', p_confidence))
$$;

create function pg_temp.hb(p_device text, p_maint boolean default false, p_ok boolean default true)
returns jsonb language sql as $$
  select jsonb_build_object(
    'schema_version', 'heartbeat.v1', 'device_id', p_device, 'sent_at', '2026-11-20T12:00:00Z',
    'software_version', '0.1.0', 'maintenance', p_maint,
    'camera', jsonb_build_object('ok', p_ok, 'last_frame_at', null, 'fps', 15.0),
    'detector', jsonb_build_object('ok', p_ok, 'last_window_at', null, 'processing_fps', 15.0),
    'storage', jsonb_build_object('free_bytes', 1000, 'total_bytes', 2000), 'queue_depth', 0)
$$;

-- act as a signed-in user (or anon when p_user is null)
create function pg_temp.login(p_user uuid) returns void language plpgsql as $$
begin
  if p_user is null then
    perform set_config('request.jwt.claims', '{"role":"anon"}', true);
    execute 'set local role anon';
  else
    perform set_config('request.jwt.claims',
      json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
    execute 'set local role authenticated';
  end if;
end $$;
