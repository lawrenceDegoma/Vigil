-- Permission tests (DESIGN §7): who can see and change what.
begin;
\ir _setup.sql.inc
select plan(25);

-- an incident in each household, plus pending reminders on household one
select private.ingest_incident_event('44444444-0000-0000-0000-000000000001',
  pg_temp.ev('55555555-0000-0000-0000-000000000001', '66666666-0000-0000-0000-000000000001',
             'incident_opened', '2026-11-20 12:00:00+00'), '2026-11-20 12:00:05+00');
select private.ingest_incident_event('44444444-0000-0000-0000-000000000002',
  pg_temp.ev('55555555-0000-0000-0000-000000000002', '66666666-0000-0000-0000-000000000002',
             'incident_opened', '2026-11-20 12:00:00+00'), '2026-11-20 12:00:05+00');
select private.schedule_reminders('2026-11-20 12:05:06+00');
insert into public.clips (id, household_id, device_id, started_at, ended_at, bytes, sha256) values
  ('77777777-0000-0000-0000-000000000001', '11111111-0000-0000-0000-000000000001',
   '44444444-0000-0000-0000-000000000001', '2026-11-20 11:59:30+00', '2026-11-20 12:02:00+00',
   1000, repeat('a', 64));

-- anon: nothing at all
select pg_temp.login(null);
select throws_ok($$ select count(*) from public.incidents $$, '42501', null, 'anon cannot read incidents');
select throws_ok($$ select count(*) from public.devices $$, '42501', null, 'anon cannot read devices');
select throws_ok($$ select public.incident_action('66666666-0000-0000-0000-000000000001', 'acknowledged') $$,
  '42501', null, 'anon cannot act on incidents');
reset role;

-- family member of household one
select pg_temp.login('00000000-0000-0000-0000-0000000000f1');
select is((select count(*)::int from public.incidents), 1, 'family sees only their household''s incidents');
select is((select count(*)::int from public.devices), 1, 'family sees only their household''s devices');
select throws_ok($$ select token_hash from public.devices $$, '42501', null, 'device token hash is never readable');
select is((select status::text from public.device_status_v), 'unknown', 'status view: never heard from = unknown');
select is((select count(*)::int from public.notification_contacts), 1, 'family sees only their own phone number');
select is((select count(*)::int from public.notifications), 0, 'delivery records are admin-only');
select is((select count(*)::int from public.clips), 1, 'family can see clip metadata');
select throws_ok($$ insert into public.notification_contacts (membership_id, phone_e164)
                    values ('22222222-0000-0000-0000-0000000000f1', '+15555550199') $$,
  '42501', null, 'family cannot add contacts');
with u as (update public.household_settings set reminder_rounds = 1 returning 1)
select is(count(*)::int, 0, 'family cannot change settings') from u;
select throws_ok($$ update public.incidents set status = 'resolved' $$, '42501', null,
  'incidents cannot be edited directly');
select throws_ok($$ select private.ingest_incident_event('44444444-0000-0000-0000-000000000001', '{}'::jsonb, now()) $$,
  '42501', null, 'users cannot call ingestion');
select throws_ok($$ select public.set_clip_preserved('77777777-0000-0000-0000-000000000001', true) $$,
  '42501', null, 'family cannot preserve clips');
select is(public.incident_action('66666666-0000-0000-0000-000000000001', 'acknowledged')::text,
  'acknowledged', 'family can acknowledge');
select throws_ok($$ select public.incident_action('66666666-0000-0000-0000-000000000002', 'acknowledged') $$,
  'P0002', null, 'cannot act on another household''s incident');
reset role;
select is((select count(*)::int from public.notifications
            where kind = 'incident_reminder' and status = 'pending'
              and incident_id = '66666666-0000-0000-0000-000000000001'), 0,
  'acknowledging cancels pending reminders');
select is((select count(*)::int from public.notifications
            where kind = 'incident_reminder' and status = 'pending'
              and incident_id = '66666666-0000-0000-0000-000000000002'), 1,
  'other households'' reminders are untouched');

-- admin of household one
select pg_temp.login('00000000-0000-0000-0000-0000000000a1');
select is((select count(*)::int from public.notification_contacts), 3, 'admin sees household contacts');
with u as (update public.household_settings set reminder_rounds = 1 returning 1)
select is(count(*)::int, 1, 'admin can change settings') from u;
select throws_ok($$ update public.household_settings set reminder_rounds = 3 $$, '23514', null,
  'at most two reminder rounds');
select is(public.set_clip_preserved('77777777-0000-0000-0000-000000000001', true), true,
  'admin can preserve clips');
reset role;
select is((select count(*)::int from public.audit_log where action in ('update', 'clip_preserved')), 2,
  'admin changes are audited');

-- outsider: signed in, no membership
select pg_temp.login('00000000-0000-0000-0000-0000000000ee');
select is((select count(*)::int from public.incidents) + (select count(*)::int from public.households), 0,
  'outsider sees nothing');
reset role;

select * from finish();
rollback;
