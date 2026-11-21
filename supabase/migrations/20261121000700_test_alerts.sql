-- Test alerts: verify that texts reach the household's phones without creating
-- an incident. No reminders follow (reminders apply only to incidents). Limited
-- to 3 test requests per household per hour; each request is idempotent on its
-- request_id and audited.

create function private.enqueue_test_alert(
  p_household uuid, p_device uuid, p_request uuid, p_scope text, p_actor uuid, p_now timestamptz
) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  v_key text := 'test:' || p_request;
  v_recent integer;
  v_n integer;
begin
  if p_scope not in ('all', 'admin') then
    return jsonb_build_object('status', 'rejected', 'reason', 'scope must be all or admin');
  end if;
  if exists (select 1 from public.notifications where dedup_key like v_key || ':%') then
    return jsonb_build_object('status', 'duplicate');
  end if;
  select count(*) into v_recent from public.audit_log
   where household_id = p_household and action = 'test_alert_requested'
     and created_at > p_now - interval '1 hour';
  if v_recent >= 3 then
    return jsonb_build_object('status', 'accepted', 'notifications', 0,
                              'reason', 'rate_limited: at most 3 test alerts per hour');
  end if;
  v_n := private.enqueue(p_household, 'test_alert', 1::smallint, null, p_device, v_key, p_now,
                         p_scope = 'admin', true);
  insert into public.audit_log (household_id, actor_id, action, target_type, target_id,
                                metadata, created_at)
  values (p_household, p_actor, 'test_alert_requested', 'device', p_device::text,
          jsonb_build_object('scope', p_scope, 'notifications', v_n, 'request_id', p_request),
          p_now);
  return jsonb_build_object('status', 'accepted', 'notifications', v_n);
end;
$$;

-- From the device (`vigil test-alert`), via device-ingest.
create function private.ingest_test_alert(p_device uuid, t jsonb, p_now timestamptz)
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  d public.devices;
begin
  select * into d from public.devices where id = p_device and revoked_at is null;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown or revoked device');
  end if;
  if t ->> 'device_id' is distinct from d.device_key then
    return jsonb_build_object('status', 'rejected', 'reason', 'device_id mismatch');
  end if;
  return private.enqueue_test_alert(d.household_id, d.id, (t ->> 'request_id')::uuid,
                                    t ->> 'scope', null, p_now);
end;
$$;

-- From the dashboard (Phase 4): administrators only. Uses the household's first
-- active device for the wording, if there is one. Sent by the next dispatch run.
create function public.send_test_alert(p_scope text default 'all')
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  v_household uuid;
  v_device uuid;
begin
  select m.household_id into v_household from public.memberships m
   where m.user_id = auth.uid() and m.active and m.role = 'admin'
   order by m.created_at limit 1;
  if v_household is null then
    raise exception 'only administrators can send test alerts' using errcode = '42501';
  end if;
  select d.id into v_device from public.devices d
   where d.household_id = v_household and d.revoked_at is null
   order by d.created_at limit 1;
  return private.enqueue_test_alert(v_household, v_device, gen_random_uuid(), p_scope,
                                    auth.uid(), now());
end;
$$;

revoke all on function private.enqueue_test_alert(uuid, uuid, uuid, text, uuid, timestamptz),
  private.ingest_test_alert(uuid, jsonb, timestamptz) from public, anon, authenticated;
revoke all on function public.send_test_alert(text) from public, anon, authenticated;
grant execute on function public.send_test_alert(text) to authenticated;
