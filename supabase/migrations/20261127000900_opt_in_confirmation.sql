-- Enrollment confirmation (toll-free verification, verbal opt-in): after a person
-- agrees in person and the administrator adds their number, that number - and only
-- that number - gets one text naming the program, the frequency, "Msg & data rates
-- may apply" and HELP/STOP. Also sent when a disabled number is re-enabled or a
-- number is changed. Not rate-capped (an administrator action, one text per number).
-- Sent by the next dispatch run; wording in supabase/functions/_shared/messages.ts.

create function private.queue_opt_in_confirmation() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_household uuid;
begin
  if not new.enabled then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.enabled and old.phone_e164 = new.phone_e164 then
    return new;
  end if;
  select m.household_id into v_household from public.memberships m
   where m.id = new.membership_id and m.active;
  if v_household is null then
    return new;
  end if;
  insert into public.notifications (household_id, contact_id, to_e164, kind, round, dedup_key,
                                    status, not_before, created_at)
  values (v_household, new.id, new.phone_e164, 'opt_in_confirmation', 1,
          'optin:' || new.id || ':' || gen_random_uuid(),
          'pending', now(), now())
  on conflict (dedup_key) do nothing;
  return new;
end;
$$;

revoke all on function private.queue_opt_in_confirmation() from public, anon, authenticated;

create trigger opt_in_confirmation
  after insert or update of enabled, phone_e164 on public.notification_contacts
  for each row execute function private.queue_opt_in_confirmation();
