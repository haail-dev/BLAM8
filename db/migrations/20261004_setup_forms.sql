-- Snapshot-checked ordinary settings forms. Apply after 20261004_setup_cards.
begin;
create or replace function setup_form_snapshot(p_tenant uuid, p_page text)
returns jsonb language plpgsql stable security invoker set search_path=public as $$
declare result jsonb := jsonb_build_object('page',p_page);
begin
  if p_page not in ('crm_fields','crm_settings','booking_details','dependants') or p_page is null then
    raise exception 'BLAM8_SETUP_INVALID';
  end if;
  if p_page in ('crm_fields','booking_details','dependants') then
    result := result || jsonb_build_object('definitions',coalesce((select jsonb_agg(to_jsonb(d) order by d.id)
      from crm_field_definitions d where tenant_id=p_tenant),'[]'::jsonb));
  end if;
  if p_page in ('booking_details','dependants') then
    result := result || jsonb_build_object('config',coalesce((select to_jsonb(c) from scheduling_config c where tenant_id=p_tenant),'{}'::jsonb));
  end if;
  if p_page='dependants' then
    result := result || jsonb_build_object('groups',coalesce((select jsonb_agg(to_jsonb(g) order by g.id) from requirement_groups g where tenant_id=p_tenant),'[]'::jsonb),
      'instances',coalesce((select jsonb_agg(to_jsonb(i) order by i.id) from requirement_instances i where tenant_id=p_tenant),'[]'::jsonb));
  end if;
  if p_page='crm_settings' then
    result := result || jsonb_build_object('quiet_days',(select crm_quiet_days from tenant_config where tenant_id=p_tenant),
      'stages',coalesce((select jsonb_agg(to_jsonb(s) order by s.id) from crm_stages s where tenant_id=p_tenant),'[]'::jsonb));
  end if;
  return result;
end $$;
revoke all on function setup_form_snapshot(uuid,text) from public;

create or replace function save_setup_form_checked(p_tenant uuid,p_page text,p_expected jsonb,p_operations jsonb)
returns void language plpgsql security invoker set search_path=public as $$
declare item jsonb; tbl text; action text; rowid uuid; fields jsonb; allowed text[];
  columns_to_write text; assignments text; affected integer; current_state jsonb;
begin
  if jsonb_typeof(p_expected) is distinct from 'object' or jsonb_typeof(p_operations) is distinct from 'array'
     or jsonb_array_length(p_operations)>2000 or octet_length(p_expected::text)>2000000
     or octet_length(p_operations::text)>2000000 then raise exception 'BLAM8_SETUP_INVALID'; end if;
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text,0));
  insert into tenant_config(tenant_id) values(p_tenant) on conflict do nothing;
  perform 1 from tenant_config where tenant_id=p_tenant for update;
  if not found then raise exception 'BLAM8_SETUP_UNAVAILABLE'; end if;
  if p_page in ('booking_details','dependants') then
    perform 1 from scheduling_config where tenant_id=p_tenant for update;
  end if;
  if p_page='crm_settings' then lock table crm_stages in share row exclusive mode; end if;
  if p_page in ('crm_fields','booking_details','dependants') then lock table crm_field_definitions in share row exclusive mode; end if;
  if p_page='dependants' then
    lock table requirement_groups in share row exclusive mode;
    lock table requirement_instances in share row exclusive mode;
  end if;
  current_state := setup_form_snapshot(p_tenant,p_page);
  if current_state is distinct from p_expected then raise exception 'BLAM8_SETTINGS_STALE'; end if;
  for item in select * from jsonb_array_elements(p_operations) loop
    tbl := item->>'table'; action := item->>'op'; rowid := (item->>'id')::uuid; fields := item->'fields';
    if jsonb_typeof(fields) is distinct from 'object' or rowid is null or action not in ('insert','update','delete') or action is null then
      raise exception 'BLAM8_SETUP_INVALID'; end if;
    allowed := null;
    if tbl='crm_stages' and p_page='crm_settings' then
      allowed := array['name','stage_type','display_order'];
    elsif tbl='tenant_config' and p_page='crm_settings' and action='update' then
      allowed := array['crm_quiet_days'];
    elsif tbl='crm_field_definitions' and p_page in ('crm_fields','booking_details','dependants') then
      allowed := array['entity','key','label','field_type','options','required','display_order','extraction_hint',
        'durability','recall_days','valid_months','probe_behaviour','staff_can_see','staff_can_see_set','ladder'];
    elsif tbl='requirement_groups' and p_page='dependants' then
      allowed := array['label','label_plural','cardinality','fields','display_order','updated_at'];
    elsif tbl='requirement_instances' and p_page='dependants' and action='update' then
      allowed := array['data'];
    elsif tbl='scheduling_config' and p_page in ('booking_details','dependants') and action='update' then
      allowed := case when p_page='booking_details' then array['intake_fields','intake_configured_at','booking_mode']
        else array['dependants_configured_at'] end;
    end if;
    if allowed is null or exists(select 1 from jsonb_object_keys(fields) k where not(k=any(allowed))) then raise exception 'BLAM8_SETUP_INVALID'; end if;
    if tbl in ('tenant_config','scheduling_config') then
      if rowid<>p_tenant then raise exception 'BLAM8_SETUP_INVALID'; end if;
      if tbl='scheduling_config' then
        if current_state->'config'='{}'::jsonb then
          insert into scheduling_config(tenant_id) values(p_tenant) on conflict do nothing;
          get diagnostics affected = row_count;
          if affected<>1 then raise exception 'BLAM8_SETTINGS_STALE'; end if;
        end if;
        fields := fields || jsonb_build_object('booking_revision',coalesce((current_state->'config'->>'booking_revision')::bigint,0)+1,'updated_at',now());
        if fields ? 'booking_mode' and fields->>'booking_mode' is distinct from coalesce(current_state->'config'->>'booking_mode','appointment') then
          lock table bookings in share row exclusive mode;
          if exists(select 1 from bookings where tenant_id=p_tenant) then raise exception 'BLAM8_SETTINGS_STALE: booking history requires migration'; end if;
        end if;
      end if;
      select string_agg(format('%1$I=source.%1$I',k),',') into assignments from jsonb_object_keys(fields) k;
      execute format('update %I target set %s from jsonb_populate_record(null::%I,$1) source where target.tenant_id=$2',tbl,assignments,tbl) using fields,p_tenant;
    elsif action='delete' then
      execute format('delete from %I where tenant_id=$1 and id=$2',tbl) using p_tenant,rowid;
    elsif action='update' then
      if fields='{}'::jsonb then raise exception 'BLAM8_SETUP_INVALID'; end if;
      select string_agg(format('%1$I=source.%1$I',k),',') into assignments from jsonb_object_keys(fields) k;
      execute format('update %I target set %s from jsonb_populate_record(null::%I,$1) source where target.tenant_id=$2 and target.id=$3',tbl,assignments,tbl) using fields,p_tenant,rowid;
    else
      fields := fields || jsonb_build_object('tenant_id',p_tenant,'id',rowid);
      select string_agg(format('%I',k),',') into columns_to_write from jsonb_object_keys(fields) k;
      execute format('insert into %I (%s) select %s from jsonb_populate_record(null::%I,$1)',tbl,columns_to_write,columns_to_write,tbl) using fields;
    end if;
    get diagnostics affected = row_count;
    if affected<>1 then raise exception 'BLAM8_SETTINGS_STALE'; end if;
  end loop;
end $$;
revoke all on function save_setup_form_checked(uuid,text,jsonb,jsonb) from public;
-- One statement returns source metadata and, for an editor, the matching text.
create or replace function knowledge_form_snapshot(p_tenant uuid,p_source text default null,p_mailbox uuid default null)
returns jsonb language sql stable security invoker set search_path=public as $$
  select coalesce(jsonb_agg(item order by item->>'source',item->>'mailbox_id'),'[]'::jsonb)
  from (select jsonb_build_object('source',source,'mailbox_id',mailbox_id,'chunks',count(*),
    'version',md5(jsonb_agg(to_jsonb(k)-'embedding' order by id)::text)) ||
    case when p_source is not null then jsonb_build_object('content',jsonb_agg(content order by created_at,source_chunk_index)) else '{}'::jsonb end as item
    from knowledge_chunks k where tenant_id=p_tenant
      and (p_source is null or (source=p_source and mailbox_id is not distinct from p_mailbox))
    group by source,mailbox_id) sources;
$$;
revoke all on function knowledge_form_snapshot(uuid,text,uuid) from public;

create or replace function save_knowledge_form_checked(p_tenant uuid,p_source text,p_new_source text,
  p_mailbox uuid,p_expected text,p_rows jsonb)
returns integer language plpgsql security invoker set search_path=public as $$
declare version text; affected integer;
begin
  if p_source is null or trim(p_source)='' or p_new_source is null or trim(p_new_source)=''
     or p_expected is null or p_expected !~ '^[0-9a-f]{32}$' then raise exception 'BLAM8_SETUP_INVALID'; end if;
  lock table knowledge_chunks in share row exclusive mode;
  select md5(coalesce(jsonb_agg(to_jsonb(k)-'embedding' order by id),'[]'::jsonb)::text) into version
    from knowledge_chunks k where tenant_id=p_tenant and source=p_source and mailbox_id is not distinct from p_mailbox;
  if version is distinct from p_expected then raise exception 'BLAM8_SETTINGS_STALE'; end if;
  if p_new_source<>p_source and exists(select 1 from knowledge_chunks where tenant_id=p_tenant
      and source=p_new_source and mailbox_id is not distinct from p_mailbox) then raise exception 'BLAM8_SETTINGS_STALE'; end if;
  if p_rows is null then
    if p_new_source<>p_source then raise exception 'BLAM8_SETUP_INVALID'; end if;
    delete from knowledge_chunks where tenant_id=p_tenant and source=p_source and mailbox_id is not distinct from p_mailbox;
    get diagnostics affected = row_count;
    return affected;
  end if;
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)=0 then raise exception 'BLAM8_SETUP_INVALID'; end if;
  -- The existing importer validates every row and inserts atomically. Both
  -- source names remain protected by this transaction's table lock.
  affected := replace_knowledge_source_atomic(p_tenant,p_new_source,p_mailbox,p_rows);
  if p_new_source<>p_source then
    delete from knowledge_chunks where tenant_id=p_tenant and source=p_source and mailbox_id is not distinct from p_mailbox;
  end if;
  return affected;
end $$;
revoke all on function save_knowledge_form_checked(uuid,text,text,uuid,text,jsonb) from public;

do $$ declare r text; begin
  foreach r in array array['authenticated','service_role'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('grant execute on function knowledge_form_snapshot(uuid,text,uuid) to %I',r);
      execute format('grant execute on function save_knowledge_form_checked(uuid,text,text,uuid,text,jsonb) to %I',r);
      execute format('grant execute on function setup_form_snapshot(uuid,text) to %I',r);
      execute format('grant execute on function save_setup_form_checked(uuid,text,jsonb,jsonb) to %I',r);
    end if;
  end loop;
end $$;
commit;
