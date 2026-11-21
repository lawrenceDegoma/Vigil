-- Test alerts: reach the right phones, create no incident or reminders, are
-- idempotent, rate-limited, audited, and admin-only from the dashboard.
begin;
\ir _setup.sql.inc
select plan(15);

create function pg_temp.test_req(p_request uuid, p_scope text default 'all', p_device text default 'pi-livingroom')
returns jsonb language sql as $$
  select jsonb_build_object('schema_version', 'test-alert.v1', 'request_id', p_request,
    'device_id', p_device, 'requested_at', '2026-11-21T12:00:00Z', 'scope', p_scope)
$$;
create function pg_temp.ask(p_request uuid, p_scope text, p_now timestamptz) returns jsonb
language sql as $$
  select private.ingest_test_alert('44444444-0000-0000-0000-000000000001', pg_temp.test_req(p_request, p_scope), p_now)
$$;

select is(pg_temp.ask('d0000000-0000-0000-0000-000000000001', 'all', '2026-11-21 12:00:00+00') ->> 'notifications', '3',
  'a test reaches every household phone');
select is((select count(*)::int from public.notifications where kind = 'test_alert' and incident_id is null
             and device_id = '44444444-0000-0000-0000-000000000001'), 3, 'tied to the device, not to an incident');
select is((select count(*)::int from public.incidents), 0, 'no incident is created');
select is(private.schedule_reminders('2026-11-21 13:00:00+00'), 0, 'no reminders follow a test');
select is(pg_temp.ask('d0000000-0000-0000-0000-000000000001', 'all', '2026-11-21 12:00:09+00') ->> 'status', 'duplicate',
  'a retried request sends nothing new');
select is(pg_temp.ask('d0000000-0000-0000-0000-000000000002', 'admin', '2026-11-21 12:01:00+00') ->> 'notifications', '1',
  'scope admin texts administrators only');
select is((select to_e164 from public.notifications where dedup_key like 'test:d0000000-0000-0000-0000-000000000002:%'),
  '+15555550101', 'the admin phone');
select is(pg_temp.ask('d0000000-0000-0000-0000-000000000003', 'all', '2026-11-21 12:02:00+00') ->> 'notifications', '3', 'third test allowed');
select is(pg_temp.ask('d0000000-0000-0000-0000-000000000004', 'all', '2026-11-21 12:03:00+00') ->> 'reason',
  'rate_limited: at most 3 test alerts per hour', 'a fourth test within the hour is refused');
select is(pg_temp.ask('d0000000-0000-0000-0000-000000000005', 'admin', '2026-11-21 13:01:00+00') ->> 'notifications', '1',
  'allowed again an hour later');
select is(private.ingest_test_alert('44444444-0000-0000-0000-000000000001',
            pg_temp.test_req('d0000000-0000-0000-0000-000000000006', 'all', 'someone-else'), '2026-11-21 14:00:00+00') ->> 'reason',
  'device_id mismatch', 'payload must match the authenticated device');
select is((select count(*)::int from public.audit_log where action = 'test_alert_requested'), 4, 'every accepted test is audited');

-- dashboard path
select pg_temp.login('00000000-0000-0000-0000-0000000000f1');
select throws_ok($$ select public.send_test_alert('all') $$, '42501', null, 'family members cannot send tests');
reset role;
select pg_temp.login(null);
select throws_ok($$ select public.send_test_alert('all') $$, '42501', null, 'anonymous callers cannot send tests');
reset role;
select pg_temp.login('00000000-0000-0000-0000-0000000000a2');
select is(public.send_test_alert('admin') ->> 'notifications', '1', 'an admin can send one from the dashboard');
reset role;

select * from finish();
rollback;
