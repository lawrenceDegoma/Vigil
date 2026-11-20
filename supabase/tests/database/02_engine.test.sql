-- Ingestion + notification engine: idempotency, reordering, fan-out, reminders,
-- rate caps, offline/recovery, and claim/lease behaviour. Fake phone numbers only.
begin;
\ir _setup.sql.inc
select plan(38);

create function pg_temp.ingest(p_event jsonb, p_now timestamptz, p_device uuid default '44444444-0000-0000-0000-000000000001')
returns jsonb language sql as $$ select private.ingest_incident_event(p_device, p_event, p_now) $$;
create function pg_temp.n(p_kind text, p_status text default null) returns int language sql as $$
  select count(*)::int from public.notifications
   where kind::text = p_kind and (p_status is null or status::text = p_status) $$;

-- ---------------------------------------------------------------- fan-out
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001',
            'incident_opened', '2026-11-20 12:00:00+00'), '2026-11-20 12:00:02+00') ->> 'notifications',
  '3', 'urgent incident alerts every household contact');
select is((select count(distinct not_before)::int from public.notifications where kind = 'incident_alert'), 1,
  'all family alerts are due at the same moment (concurrent fan-out)');
select is((select count(*)::int from public.notifications where to_e164 = '+15555550201'), 0,
  'other households are not alerted');
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001',
            'incident_opened', '2026-11-20 12:00:00+00'), '2026-11-20 12:00:09+00') ->> 'status',
  'duplicate', 'replayed event is a duplicate');
select is(pg_temp.n('incident_alert'), 3, 'replay creates no extra notifications');
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000001',
            'incident_updated', '2026-11-20 12:00:30+00', p_confidence => 0.9), '2026-11-20 12:00:31+00') ->> 'notifications',
  '0', 'updates to an alerted incident do not re-alert');

-- ------------------------------------------------------------- reordering
select pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000004', 'b0000000-0000-0000-0000-000000000001',
  'incident_ended', '2026-11-20 12:02:00+00', p_ended => '2026-11-20 12:01:50+00', p_confidence => 0.5), '2026-11-20 12:02:01+00');
select pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000003', 'b0000000-0000-0000-0000-000000000001',
  'incident_updated', '2026-11-20 12:01:00+00', p_confidence => 0.95), '2026-11-20 12:02:02+00');
select is((select ended_at from public.incidents where id = 'b0000000-0000-0000-0000-000000000001'),
  '2026-11-20 12:01:50+00'::timestamptz, 'a late older update does not re-open an ended incident');
select is((select confidence from public.incidents where id = 'b0000000-0000-0000-0000-000000000001'),
  0.95::real, 'confidence only ratchets up, even from late events');
select pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000005', 'b0000000-0000-0000-0000-000000000001',
  'incident_updated', '2026-11-20 12:02:30+00'), '2026-11-20 12:02:31+00');
select is((select ended_at from public.incidents where id = 'b0000000-0000-0000-0000-000000000001'), null,
  'a newer update resumes the incident');
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000006', 'b0000000-0000-0000-0000-000000000009',
            'incident_ended', '2026-11-20 12:03:00+00', p_ended => '2026-11-20 12:02:59+00'), '2026-11-20 12:03:01+00') ->> 'new',
  'true', 'an incident can be created by any event type (lost opened event)');

-- ---------------------------------------------------------------- rejects
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000007', 'b0000000-0000-0000-0000-000000000007',
            'incident_opened', '2026-11-20 12:00:00+00', p_device => 'someone-else'), '2026-11-20 12:00:01+00') ->> 'reason',
  'device_id mismatch', 'payload device_id must match the authenticated device');
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000008', 'b0000000-0000-0000-0000-000000000001',
            'incident_updated', '2026-11-20 12:05:00+00'), '2026-11-20 12:05:01+00',
            '44444444-0000-0000-0000-000000000002') ->> 'reason',
  'incident belongs to another device', 'a device cannot touch another device''s incident');
select is((select count(*)::int from public.device_events where event_id = 'a0000000-0000-0000-0000-000000000008'), 0,
  'rejected events are not recorded');

-- ------------------------------------------------- warnings and escalation
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000010', 'b0000000-0000-0000-0000-000000000010',
            'incident_opened', '2026-11-20 13:00:00+00', p_signal => 'vomiting', p_severity => 'warning'),
            '2026-11-20 13:00:01+00') ->> 'notifications', '3', 'warnings alert when enabled');
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000011', 'b0000000-0000-0000-0000-000000000010',
            'incident_updated', '2026-11-20 13:00:20+00', p_signal => 'vomiting', p_severity => 'urgent'),
            '2026-11-20 13:00:21+00') ->> 'notifications', '3', 'escalation to urgent alerts again');
update public.household_settings set warning_alerts_enabled = false
 where household_id = '11111111-0000-0000-0000-000000000001';
select is(pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000012', 'b0000000-0000-0000-0000-000000000012',
            'incident_opened', '2026-11-20 13:10:00+00', p_signal => 'vomiting', p_severity => 'warning'),
            '2026-11-20 13:10:01+00') ->> 'notifications', '0', 'warnings can be switched off');
update public.household_settings set warning_alerts_enabled = true
 where household_id = '11111111-0000-0000-0000-000000000001';

-- -------------------------------------------------------------- reminders
-- incident ...01 alerted at 12:00:02; interval 300 s, two rounds
select is(private.schedule_reminders('2026-11-20 12:05:01+00'), 0, 'no reminder before the interval');
select is(private.schedule_reminders('2026-11-20 12:05:02+00'), 3, 'round 2 at +5 min to every contact');
select is(private.schedule_reminders('2026-11-20 12:05:30+00'), 0, 'reminders are idempotent');
select private.schedule_reminders('2026-11-20 12:10:02+00');
select is((select count(*)::int from public.notifications where incident_id = 'b0000000-0000-0000-0000-000000000001'
             and kind = 'incident_reminder' and round = 3), 3, 'round 3 at +10 min');
select private.schedule_reminders('2026-11-20 12:30:00+00');
select is((select count(*)::int from public.notifications where incident_id = 'b0000000-0000-0000-0000-000000000001'
             and kind = 'incident_reminder'), 6, 'never more than two reminder rounds');
select is((select count(*)::int from public.notifications where incident_id = 'b0000000-0000-0000-0000-000000000009'
             and kind = 'incident_reminder'), 6, 'an ended but unacknowledged urgent incident still gets reminders');
select is((select max(round)::int from public.notifications where kind = 'incident_reminder'), 3, 'rounds are numbered 2..3');
select is((select count(*)::int from public.notifications n join public.incidents i on i.id = n.incident_id
            where n.kind = 'incident_reminder' and i.severity = 'warning'), 0, 'warnings never get reminders');

-- --------------------------------------------------------------- rate cap
update public.household_settings set max_sms_per_hour = 5 where household_id = '11111111-0000-0000-0000-000000000001';
select pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000020', 'b0000000-0000-0000-0000-000000000020',
  'incident_opened', '2026-11-20 12:40:00+00', p_signal => 'vomiting', p_severity => 'warning'), '2026-11-20 12:40:01+00');
select is((select count(*)::int from public.notifications where incident_id = 'b0000000-0000-0000-0000-000000000020'
             and status = 'cancelled' and last_error = 'rate_limited'), 3, 'warnings are capped per hour');
select pg_temp.ingest(pg_temp.ev('a0000000-0000-0000-0000-000000000021', 'b0000000-0000-0000-0000-000000000021',
  'incident_opened', '2026-11-20 12:41:00+00'), '2026-11-20 12:41:01+00');
select is((select count(*)::int from public.notifications where incident_id = 'b0000000-0000-0000-0000-000000000021'
             and status = 'pending'), 3, 'first urgent alerts are never capped');
update public.household_settings set max_sms_per_hour = 40 where household_id = '11111111-0000-0000-0000-000000000001';

-- ---------------------------------------------------- offline and recovery
select private.ingest_heartbeat('44444444-0000-0000-0000-000000000001', pg_temp.hb('pi-livingroom'), '2026-11-20 14:00:00+00');
select is(private.check_stale_devices('2026-11-20 14:04:59+00'), 0, 'not offline within the grace period');
select is(private.check_stale_devices('2026-11-20 14:05:01+00'), 1, 'offline notice goes to the admin only');
select is((select to_e164 from public.notifications where kind = 'device_offline'), '+15555550101', 'admin phone');
select is(private.check_stale_devices('2026-11-20 14:20:00+00'), 0, 'one offline notice per outage');
select is(private.ingest_heartbeat('44444444-0000-0000-0000-000000000001', pg_temp.hb('pi-livingroom'),
            '2026-11-20 14:30:00+00') ->> 'notifications', '1', 'recovery notice to the admin');
select private.ingest_heartbeat('44444444-0000-0000-0000-000000000001', pg_temp.hb('pi-livingroom', p_maint => true), '2026-11-20 15:00:00+00');
select is(private.check_stale_devices('2026-11-20 16:00:00+00'), 0, 'no offline notice while in maintenance');
select is(private.ingest_heartbeat('44444444-0000-0000-0000-000000000001', pg_temp.hb('pi-livingroom', p_ok => false),
            '2026-11-20 16:01:00+00') ->> 'device_status', 'degraded', 'camera/detector failure reports degraded');

-- ----------------------------------------------------------- claim / lease
create temp table claimed as select * from private.claim_notifications('2026-11-20 16:02:00+00', 1000);
select ok((select count(*) from claimed) > 0, 'due notifications are claimed');
select is((select count(*)::int from private.claim_notifications('2026-11-20 16:02:30+00', 1000)), 0,
  'claimed rows are leased (no double send)');
select is(private.complete_notification((select min(id) from claimed), false, 'mock', null, null, 'timeout',
            '2026-11-20 16:03:00+00', '2026-11-20 16:02:01+00')::text, 'pending', 'retryable failure goes back to pending');
select is(private.complete_notification((select max(id) from claimed), true, 'mock', 'm-1', 'body', null, null,
            '2026-11-20 16:02:01+00')::text, 'sent', 'success is recorded');
update public.notification_contacts set enabled = false where id = '33333333-0000-0000-0000-0000000000f2';
select private.claim_notifications('2026-11-20 16:10:00+00', 1000);
select is((select count(*)::int from public.notifications where to_e164 = '+15555550103' and status = 'pending'), 0,
  'messages to a disabled contact are cancelled');

select * from finish();
rollback;
