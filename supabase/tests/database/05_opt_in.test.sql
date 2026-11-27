-- Enrollment confirmation: adding a number (or re-enabling it, or changing it)
-- queues exactly one confirmation text to that number only; disabled numbers and
-- inactive members get none; the dispatcher can claim it without an incident.
begin;
\ir _setup.sql.inc
select plan(12);

-- the administrator adds a family member's number through the API (RLS path)
select pg_temp.login('00000000-0000-0000-0000-0000000000a1');
insert into public.notification_contacts (membership_id, phone_e164) values
  ('22222222-0000-0000-0000-0000000000f2', '+15555550109');
reset role;

select is((select count(*)::int from public.notifications where kind = 'opt_in_confirmation'), 1,
  'adding a number queues one confirmation');
select is((select to_e164 from public.notifications where kind = 'opt_in_confirmation'), '+15555550109',
  'sent to the new number only');
select is((select household_id from public.notifications where kind = 'opt_in_confirmation'),
  '11111111-0000-0000-0000-000000000001'::uuid, 'in the member''s household');
select ok((select incident_id is null and status = 'pending' from public.notifications
            where kind = 'opt_in_confirmation'), 'pending, not tied to an incident');

update public.notification_contacts set enabled = true where membership_id = '22222222-0000-0000-0000-0000000000f2' and phone_e164 in ('+15555550109', '+15555550110');
select is((select count(*)::int from public.notifications where kind = 'opt_in_confirmation'), 1,
  'an update that changes nothing sends nothing');

update public.notification_contacts set enabled = false where membership_id = '22222222-0000-0000-0000-0000000000f2' and phone_e164 in ('+15555550109', '+15555550110');
select is((select count(*)::int from public.notifications where kind = 'opt_in_confirmation'), 1,
  'disabling sends nothing');

update public.notification_contacts set enabled = true where membership_id = '22222222-0000-0000-0000-0000000000f2' and phone_e164 in ('+15555550109', '+15555550110');
select is((select count(*)::int from public.notifications where kind = 'opt_in_confirmation'), 2,
  're-enabling sends a new confirmation');

update public.notification_contacts set phone_e164 = '+15555550110' where membership_id = '22222222-0000-0000-0000-0000000000f2' and phone_e164 in ('+15555550109', '+15555550110');
select is((select count(*)::int from public.notifications where kind = 'opt_in_confirmation' and to_e164 = '+15555550110'), 1,
  'a changed number gets its own confirmation');

insert into public.notification_contacts (membership_id, phone_e164, enabled) values
  ('22222222-0000-0000-0000-0000000000f1', '+15555550111', false);
select is((select count(*)::int from public.notifications where to_e164 = '+15555550111'), 0,
  'a number added disabled gets nothing');

update public.memberships set active = false where id = '22222222-0000-0000-0000-0000000000f1';
insert into public.notification_contacts (membership_id, phone_e164) values
  ('22222222-0000-0000-0000-0000000000f1', '+15555550112');
select is((select count(*)::int from public.notifications where to_e164 = '+15555550112'), 0,
  'an inactive member gets nothing');

select is((select count(*)::int from private.claim_notifications(now() + interval '1 second', 10)
            where kind = 'opt_in_confirmation' and incident_id is null), 3,
  'the dispatcher claims confirmations without an incident');

select pg_temp.login('00000000-0000-0000-0000-0000000000f1');
select throws_ok($$ insert into public.notification_contacts (membership_id, phone_e164)
                    values ('22222222-0000-0000-0000-0000000000f2', '+15555550113') $$,
  '42501', null, 'family members cannot add numbers (so cannot trigger texts)');
reset role;

select * from finish();
rollback;
