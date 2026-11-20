-- Authorization (DESIGN §7). Least privilege:
--   * anon: nothing.
--   * authenticated: SELECT on their own household's rows (RLS), a few admin-only
--     column updates, and RPCs that re-check membership server-side.
--   * Devices never use these roles: ingestion runs in Edge Functions over a direct
--     database connection and calls `private.*` functions (next migration).
-- Frontend role checks are UX only; this file is the enforcement.

-- ------------------------------------------------------------------ helpers
create function private.is_member(hh uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.memberships m
    where m.household_id = hh and m.user_id = (select auth.uid()) and m.active
  )
$$;

create function private.is_admin(hh uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.memberships m
    where m.household_id = hh and m.user_id = (select auth.uid()) and m.active
      and m.role = 'admin'
  )
$$;

create function private.own_membership(mid uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.memberships m
    where m.id = mid and m.user_id = (select auth.uid()) and m.active
  )
$$;

create function private.membership_household(mid uuid) returns uuid
language sql stable security definer set search_path = '' as $$
  select m.household_id from public.memberships m where m.id = mid
$$;

-- ------------------------------------------------------------------- grants
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated, public;
revoke all on schema private from public, anon, authenticated;
revoke all on all functions in schema private from public, anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated, public;
alter default privileges in schema private revoke all on functions from public, anon, authenticated;

-- RLS policies call these helpers, so the role needs schema usage + execute on them only.
grant usage on schema private to authenticated;
grant execute on function private.is_member(uuid), private.is_admin(uuid),
  private.own_membership(uuid), private.membership_household(uuid) to authenticated;

grant select on public.households, public.household_settings, public.memberships,
  public.notification_contacts, public.device_health, public.incidents,
  public.incident_actions, public.clips, public.clip_incidents, public.notifications,
  public.device_events, public.audit_log to authenticated;
-- token_hash is never readable by users.
grant select (id, household_id, device_key, name, subject_name, upload_clips,
  last_heartbeat_at, last_status, offline_since, software_version, created_at, revoked_at)
  on public.devices to authenticated;
grant select on public.device_status_v to authenticated;

-- Admin-editable columns (RLS below restricts rows to admins).
grant update (name) on public.households to authenticated;
grant update (timezone, warning_alerts_enabled, reminder_rounds, reminder_interval_seconds,
  offline_grace_seconds, notify_recovery, max_sms_per_hour, retention_days)
  on public.household_settings to authenticated;
grant update (name, subject_name, upload_clips) on public.devices to authenticated;
grant insert (membership_id, phone_e164, enabled), update (phone_e164, enabled), delete
  on public.notification_contacts to authenticated;

-- ---------------------------------------------------------------------- RLS
alter table public.households enable row level security;
alter table public.household_settings enable row level security;
alter table public.memberships enable row level security;
alter table public.notification_contacts enable row level security;
alter table public.devices enable row level security;
alter table public.device_health enable row level security;
alter table public.incidents enable row level security;
alter table public.device_events enable row level security;
alter table public.incident_actions enable row level security;
alter table public.clips enable row level security;
alter table public.clip_incidents enable row level security;
alter table public.notifications enable row level security;
alter table public.audit_log enable row level security;

create policy households_select on public.households for select to authenticated
  using ((select private.is_member(id)));
create policy households_update on public.households for update to authenticated
  using ((select private.is_admin(id))) with check ((select private.is_admin(id)));

create policy settings_select on public.household_settings for select to authenticated
  using ((select private.is_member(household_id)));
create policy settings_update on public.household_settings for update to authenticated
  using ((select private.is_admin(household_id)))
  with check ((select private.is_admin(household_id)));

create policy memberships_select on public.memberships for select to authenticated
  using ((select private.is_member(household_id)));

-- Phone numbers: each member sees their own; admins see and manage all.
create policy contacts_select on public.notification_contacts for select to authenticated
  using (
    (select private.own_membership(membership_id))
    or (select private.is_admin(private.membership_household(membership_id)))
  );
create policy contacts_insert on public.notification_contacts for insert to authenticated
  with check ((select private.is_admin(private.membership_household(membership_id))));
create policy contacts_update on public.notification_contacts for update to authenticated
  using ((select private.is_admin(private.membership_household(membership_id))))
  with check ((select private.is_admin(private.membership_household(membership_id))));
create policy contacts_delete on public.notification_contacts for delete to authenticated
  using ((select private.is_admin(private.membership_household(membership_id))));

create policy devices_select on public.devices for select to authenticated
  using ((select private.is_member(household_id)));
create policy devices_update on public.devices for update to authenticated
  using ((select private.is_admin(household_id)))
  with check ((select private.is_admin(household_id)));

create policy device_health_select on public.device_health for select to authenticated
  using ((select private.is_member(
    (select d.household_id from public.devices d where d.id = device_id))));

create policy incidents_select on public.incidents for select to authenticated
  using ((select private.is_member(household_id)));

create policy incident_actions_select on public.incident_actions for select to authenticated
  using ((select private.is_member(household_id)));

create policy clips_select on public.clips for select to authenticated
  using ((select private.is_member(household_id)));
create policy clip_incidents_select on public.clip_incidents for select to authenticated
  using ((select private.is_member(
    (select c.household_id from public.clips c where c.id = clip_id))));

-- Delivery records, raw device events and the audit log are admin-only.
create policy notifications_select on public.notifications for select to authenticated
  using ((select private.is_admin(household_id)));
create policy device_events_select on public.device_events for select to authenticated
  using ((select private.is_admin(
    (select d.household_id from public.devices d where d.id = device_id))));
create policy audit_log_select on public.audit_log for select to authenticated
  using ((select private.is_admin(household_id)));

-- ----------------------------------------------------------------- user RPCs
-- Any active member may acknowledge / respond / check / resolve / label an
-- incident (DESIGN §6–7). Leaving 'open' cancels pending reminders.
create function public.incident_action(p_incident uuid, p_action text, p_note text default null)
returns public.incident_status
language plpgsql security definer set search_path = '' as $$
declare
  v_inc public.incidents;
  v_status public.incident_status;
begin
  select * into v_inc from public.incidents where id = p_incident for update;
  if not found or not private.is_member(v_inc.household_id) then
    raise exception 'incident not found' using errcode = 'P0002';
  end if;
  if p_action not in ('acknowledged', 'responding', 'checked', 'resolved', 'false_positive',
                      'needs_review', 'note') then
    raise exception 'invalid action %', p_action using errcode = '22023';
  end if;
  if p_action = 'note' and coalesce(length(trim(p_note)), 0) = 0 then
    raise exception 'a note needs text' using errcode = '22023';
  end if;

  insert into public.incident_actions (incident_id, household_id, actor_id, action, note)
  values (p_incident, v_inc.household_id, auth.uid(), p_action, nullif(trim(p_note), ''));

  v_status := v_inc.status;
  if p_action <> 'note' then
    v_status := p_action::public.incident_status;
    update public.incidents
       set status = v_status, status_changed_at = now(), status_changed_by = auth.uid(),
           updated_at = now()
     where id = p_incident;
    if v_status <> 'open' then
      update public.notifications
         set status = 'cancelled', last_error = 'incident ' || v_status::text
       where incident_id = p_incident and kind = 'incident_reminder' and status = 'pending';
    end if;
  end if;
  return v_status;
end;
$$;

-- Only admins may preserve clips (exempt from ordinary expiry).
create function public.set_clip_preserved(p_clip uuid, p_preserved boolean)
returns boolean
language plpgsql security definer set search_path = '' as $$
declare
  v_hh uuid;
begin
  select household_id into v_hh from public.clips where id = p_clip for update;
  if not found or not private.is_admin(v_hh) then
    raise exception 'clip not found or not permitted' using errcode = '42501';
  end if;
  update public.clips
     set preserved_at = case when p_preserved then now() end,
         preserved_by = case when p_preserved then auth.uid() end
   where id = p_clip;
  insert into public.audit_log (household_id, actor_id, action, target_type, target_id)
  values (v_hh, auth.uid(), case when p_preserved then 'clip_preserved' else 'clip_unpreserved' end,
          'clip', p_clip::text);
  return p_preserved;
end;
$$;

-- Supabase's default privileges grant EXECUTE on new functions to anon; revoke explicitly.
revoke all on function public.incident_action(uuid, text, text) from public, anon, authenticated;
revoke all on function public.set_clip_preserved(uuid, boolean) from public, anon, authenticated;
grant execute on function public.incident_action(uuid, text, text) to authenticated;
grant execute on function public.set_clip_preserved(uuid, boolean) to authenticated;

-- Audit admin edits to settings and contacts.
create function private.audit_row_change() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_hh uuid;
begin
  if tg_table_name = 'notification_contacts' then
    v_hh := private.membership_household(coalesce(new.membership_id, old.membership_id));
  else
    v_hh := coalesce(new.household_id, old.household_id);
  end if;
  insert into public.audit_log (household_id, actor_id, action, target_type, target_id)
  values (v_hh, auth.uid(), lower(tg_op), tg_table_name,
          coalesce(to_jsonb(new) ->> 'id', to_jsonb(old) ->> 'id',
                   to_jsonb(new) ->> 'household_id'));
  return coalesce(new, old);
end;
$$;

revoke all on function private.audit_row_change() from public, anon, authenticated;

create trigger audit_settings after update on public.household_settings
  for each row execute function private.audit_row_change();
create trigger audit_contacts after insert or update or delete on public.notification_contacts
  for each row execute function private.audit_row_change();
create trigger audit_devices after update on public.devices
  for each row when (old.name is distinct from new.name
                     or old.subject_name is distinct from new.subject_name
                     or old.upload_clips is distinct from new.upload_clips)
  execute function private.audit_row_change();
