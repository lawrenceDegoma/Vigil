-- Incident clip upload and viewing (ADR-010 option b).
-- * Only clips of ALERTED incidents are accepted; shadow/normal footage never uploads.
-- * The bucket is private with no storage policies for users: nobody can list or read
--   objects directly. Viewing goes through the clip-url function, which checks
--   membership and returns short-lived signed URLs (download links for admins only).
-- * Large clips arrive as several playable parts (Free plan: 50 MB per file).

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('incident-clips', 'incident-clips', false, 52428800, array['video/mp4'])
on conflict (id) do nothing;

alter table public.clips drop column storage_path;  -- moved to clip_files (one row per part)
alter table public.clips add column part_count smallint not null default 1
  check (part_count between 1 and 50);

create table public.clip_files (
  clip_id uuid not null references public.clips on delete cascade,
  part_index smallint not null check (part_index >= 0),
  storage_path text not null unique,
  bytes bigint not null check (bytes > 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  uploaded_at timestamptz,
  primary key (clip_id, part_index)
);

alter table public.clip_files enable row level security;
revoke all on public.clip_files from anon, authenticated;
grant select (clip_id, part_index, bytes, uploaded_at) on public.clip_files to authenticated;
create policy clip_files_select on public.clip_files for select to authenticated
  using ((select private.is_member(
    (select c.household_id from public.clips c where c.id = clip_id))));

-- Register a clip and its parts for upload. Idempotent; returns each part's path and
-- whether it is already uploaded.
create function private.register_clip(p_device uuid, c jsonb, p_now timestamptz)
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  d public.devices;
  v_clip uuid := (c ->> 'id')::uuid;
  v_incidents uuid[];
  v_existing public.clips;
  v_parts jsonb := c -> 'parts';
  v_count integer;
  v_path text;
  p jsonb;
begin
  select * into d from public.devices where id = p_device and revoked_at is null;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown or revoked device');
  end if;
  if not d.upload_clips then
    return jsonb_build_object('status', 'disabled');
  end if;

  select array_agg(x::uuid) into v_incidents from jsonb_array_elements_text(c -> 'incident_ids') x;
  if v_incidents is null or exists (
    select 1 from unnest(v_incidents) i
     where not exists (select 1 from public.incidents n where n.id = i and n.device_id = d.id)
  ) then
    -- Incident events are delivered before clips; a missing one means "not yet" or "not ours".
    return jsonb_build_object('status', 'unknown_incident');
  end if;
  if not exists (select 1 from public.incidents n
                  where n.id = any (v_incidents) and n.alerted_severity is not null) then
    return jsonb_build_object('status', 'not_eligible');  -- never alerted: stays on device
  end if;

  v_count := jsonb_array_length(v_parts);
  if v_count is null or v_count < 1 or v_count > 50 then
    return jsonb_build_object('status', 'rejected', 'reason', 'bad parts');
  end if;

  select * into v_existing from public.clips where id = v_clip;
  if found and v_existing.device_id <> d.id then
    return jsonb_build_object('status', 'rejected', 'reason', 'clip belongs to another device');
  end if;
  if found and v_existing.part_count <> v_count then
    return jsonb_build_object('status', 'rejected', 'reason', 'part count changed');
  end if;

  insert into public.clips (id, household_id, device_id, started_at, ended_at, bytes, sha256,
                            part_count, created_at)
  values (v_clip, d.household_id, d.id, (c ->> 'started_at')::timestamptz,
          (c ->> 'ended_at')::timestamptz, (c ->> 'bytes')::bigint, c ->> 'sha256', v_count, p_now)
  on conflict (id) do nothing;

  insert into public.clip_incidents (clip_id, incident_id)
  select v_clip, i from unnest(v_incidents) i
  on conflict do nothing;

  for p in select * from jsonb_array_elements(v_parts) loop
    v_path := d.household_id || '/' || d.id || '/' || v_clip || '/part-'
              || lpad((p ->> 'index'), 3, '0') || '.mp4';
    insert into public.clip_files (clip_id, part_index, storage_path, bytes, sha256)
    values (v_clip, (p ->> 'index')::smallint, v_path, (p ->> 'bytes')::bigint, p ->> 'sha256')
    on conflict (clip_id, part_index) do nothing;
  end loop;

  return jsonb_build_object(
    'status', 'ok',
    'parts', (select jsonb_agg(jsonb_build_object(
                'index', f.part_index, 'path', f.storage_path, 'bytes', f.bytes,
                'uploaded', f.uploaded_at is not null) order by f.part_index)
                from public.clip_files f where f.clip_id = v_clip));
end;
$$;

-- Mark one part uploaded (the caller has verified the object's size in storage).
-- When every part is in, the clip becomes viewable and its cloud expiry starts.
create function private.complete_clip_part(p_device uuid, p_clip uuid, p_index smallint,
                                           p_now timestamptz)
returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  v_clip public.clips;
  v_days integer;
  v_missing integer;
begin
  select * into v_clip from public.clips where id = p_clip and device_id = p_device for update;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown clip');
  end if;
  update public.clip_files set uploaded_at = coalesce(uploaded_at, p_now)
   where clip_id = p_clip and part_index = p_index;
  if not found then
    return jsonb_build_object('status', 'rejected', 'reason', 'unknown part');
  end if;
  select count(*) into v_missing from public.clip_files
   where clip_id = p_clip and uploaded_at is null;
  if v_missing = 0 and v_clip.uploaded_at is null then
    select retention_days into v_days from public.household_settings
     where household_id = v_clip.household_id;
    update public.clips
       set uploaded_at = p_now,
           expires_at = p_now + make_interval(days => coalesce(v_days, 30))
     where id = p_clip;
  end if;
  return jsonb_build_object('status', 'ok', 'complete', v_missing = 0);
end;
$$;

create function private.clip_part_path(p_device uuid, p_clip uuid, p_index smallint)
returns table (storage_path text, bytes bigint)
language sql stable security definer set search_path = '' as $$
  select f.storage_path, f.bytes
    from public.clip_files f join public.clips c on c.id = f.clip_id
   where f.clip_id = p_clip and f.part_index = p_index and c.device_id = p_device
$$;

-- What a signed-in user may see of a clip: their role, and the part paths, only if
-- they are an active member of the clip's household and the clip is fully uploaded.
create function private.clip_access(p_user uuid, p_clip uuid)
returns table (household_id uuid, role public.member_role, part_index smallint, storage_path text)
language sql stable security definer set search_path = '' as $$
  select c.household_id, m.role, f.part_index, f.storage_path
    from public.clips c
    join public.memberships m on m.household_id = c.household_id
                              and m.user_id = p_user and m.active
    join public.clip_files f on f.clip_id = c.id
   where c.id = p_clip and c.uploaded_at is not null
   order by f.part_index
$$;

create function private.audit(p_household uuid, p_actor uuid, p_action text, p_type text,
                              p_target text, p_meta jsonb default '{}'::jsonb)
returns void
language sql security definer set search_path = '' as $$
  insert into public.audit_log (household_id, actor_id, action, target_type, target_id, metadata)
  values (p_household, p_actor, p_action, p_type, p_target, coalesce(p_meta, '{}'::jsonb))
$$;

-- Cloud retention: expired, unpreserved clips (their storage objects are deleted by
-- the dispatch function, then the rows via forget_clip).
create function private.expired_clips(p_now timestamptz, p_limit integer)
returns table (clip_id uuid, storage_path text)
language sql stable security definer set search_path = '' as $$
  select c.id, f.storage_path
    from public.clips c join public.clip_files f on f.clip_id = c.id
   where c.expires_at < p_now and c.preserved_at is null
     and c.id in (select c2.id from public.clips c2
                   where c2.expires_at < p_now and c2.preserved_at is null
                   order by c2.expires_at limit p_limit)
$$;

create function private.forget_clip(p_clip uuid) returns void
language sql security definer set search_path = '' as $$
  delete from public.clips where id = p_clip and preserved_at is null
$$;

revoke all on all functions in schema private from public, anon, authenticated;
grant execute on function private.is_member(uuid), private.is_admin(uuid),
  private.own_membership(uuid), private.membership_household(uuid),
  private.valid_timezone(text) to authenticated;
