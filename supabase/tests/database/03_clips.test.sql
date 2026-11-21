-- Clip upload eligibility, ownership, access and cloud retention (ADR-010).
begin;
\ir _setup.sql.inc
select plan(20);

create function pg_temp.clip(p_id uuid, p_incidents uuid[], p_parts int default 1) returns jsonb
language sql as $$
  select jsonb_build_object(
    'id', p_id, 'incident_ids', to_jsonb(p_incidents),
    'started_at', '2026-11-20T11:59:30Z', 'ended_at', '2026-11-20T12:03:00Z',
    'bytes', 1000 * p_parts, 'sha256', repeat('a', 64),
    'parts', (select jsonb_agg(jsonb_build_object('index', i, 'bytes', 1000, 'sha256', repeat('b', 64)))
                from generate_series(0, p_parts - 1) i))
$$;

-- alerted urgent incident on device 1; an un-alerted warning (alerts off) too
select private.ingest_incident_event('44444444-0000-0000-0000-000000000001',
  pg_temp.ev('a0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001',
             'incident_opened', '2026-11-20 12:00:00+00'), '2026-11-20 12:00:01+00');
update public.household_settings set warning_alerts_enabled = false
 where household_id = '11111111-0000-0000-0000-000000000001';
select private.ingest_incident_event('44444444-0000-0000-0000-000000000001',
  pg_temp.ev('a0000000-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002',
             'incident_opened', '2026-11-20 12:10:00+00', p_signal => 'vomiting', p_severity => 'warning'),
  '2026-11-20 12:10:01+00');

-- ------------------------------------------------------------- eligibility
select is(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000002', array['b0000000-0000-0000-0000-000000000002'::uuid]),
            '2026-11-20 12:20:00+00') ->> 'status', 'not_eligible', 'clips of never-alerted incidents stay on the device');
select is(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000009', array['b0000000-0000-0000-0000-000000000099'::uuid]),
            '2026-11-20 12:20:00+00') ->> 'status', 'unknown_incident', 'unknown incident: retry later');
select is(private.register_clip('44444444-0000-0000-0000-000000000002',
            pg_temp.clip('c0000000-0000-0000-0000-000000000003', array['b0000000-0000-0000-0000-000000000001'::uuid]),
            '2026-11-20 12:20:00+00') ->> 'status', 'unknown_incident', 'a device cannot attach clips to another device''s incident');
update public.devices set upload_clips = false where id = '44444444-0000-0000-0000-000000000001';
select is(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000001', array['b0000000-0000-0000-0000-000000000001'::uuid]),
            '2026-11-20 12:20:00+00') ->> 'status', 'disabled', 'admin can keep clips local-only');
update public.devices set upload_clips = true where id = '44444444-0000-0000-0000-000000000001';

-- ------------------------------------------------------- register/complete
select is(jsonb_array_length(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000001', array['b0000000-0000-0000-0000-000000000001'::uuid], 2),
            '2026-11-20 12:20:00+00') -> 'parts'), 2, 'two parts registered');
select is((select storage_path from public.clip_files where clip_id = 'c0000000-0000-0000-0000-000000000001' and part_index = 1),
  '11111111-0000-0000-0000-000000000001/44444444-0000-0000-0000-000000000001/c0000000-0000-0000-0000-000000000001/part-001.mp4',
  'storage path is household/device/clip/part');
select is(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000001', array['b0000000-0000-0000-0000-000000000001'::uuid], 2),
            '2026-11-20 12:21:00+00') ->> 'status', 'ok', 're-registering is idempotent');
select is(private.register_clip('44444444-0000-0000-0000-000000000001',
            pg_temp.clip('c0000000-0000-0000-0000-000000000001', array['b0000000-0000-0000-0000-000000000001'::uuid], 3),
            '2026-11-20 12:21:00+00') ->> 'reason', 'part count changed', 'parts cannot change after registration');
select is(private.complete_clip_part('44444444-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000001', 0::smallint,
            '2026-11-20 12:22:00+00') ->> 'status', 'rejected', 'another device cannot complete our clip');
select is((private.complete_clip_part('44444444-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 0::smallint,
            '2026-11-20 12:22:00+00') ->> 'complete')::boolean, false, 'one of two parts: not complete');
select is((select count(*)::int from private.clip_access('00000000-0000-0000-0000-0000000000f1', 'c0000000-0000-0000-0000-000000000001')), 0,
  'partially uploaded clips are not viewable');
select is((private.complete_clip_part('44444444-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 1::smallint,
            '2026-11-20 12:23:00+00') ->> 'complete')::boolean, true, 'all parts: complete');
select is((select expires_at from public.clips where id = 'c0000000-0000-0000-0000-000000000001'),
  '2026-12-20 12:23:00+00'::timestamptz, 'cloud expiry = upload time + household retention (30 days)');

-- ------------------------------------------------------------------ access
select is((select count(*)::int from private.clip_access('00000000-0000-0000-0000-0000000000f1', 'c0000000-0000-0000-0000-000000000001')), 2,
  'family member gets both parts');
select is((select distinct role::text from private.clip_access('00000000-0000-0000-0000-0000000000f1', 'c0000000-0000-0000-0000-000000000001')),
  'family', 'role is reported so only admins get downloads');
select is((select count(*)::int from private.clip_access('00000000-0000-0000-0000-0000000000a2', 'c0000000-0000-0000-0000-000000000001')), 0,
  'another household''s admin gets nothing');
select is((select count(*)::int from private.clip_access('00000000-0000-0000-0000-0000000000ee', 'c0000000-0000-0000-0000-000000000001')), 0,
  'outsider gets nothing');
select pg_temp.login('00000000-0000-0000-0000-0000000000f1');
select throws_ok($$ select storage_path from public.clip_files $$, '42501', null, 'storage paths are not readable by users');
reset role;

-- --------------------------------------------------------------- retention
select is((select count(*)::int from private.expired_clips('2026-12-21 00:00:00+00', 10)), 2, 'expired clip parts are listed');
update public.clips set preserved_at = now() where id = 'c0000000-0000-0000-0000-000000000001';
select is((select count(*)::int from private.expired_clips('2026-12-21 00:00:00+00', 10)), 0, 'preserved clips never expire');

select * from finish();
rollback;
