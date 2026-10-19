begin;
create or replace function change_setup_card(
  p_tenant uuid, p_page text, p_revision bigint, p_card jsonb,
  p_expected_settings jsonb default null, p_settings_patch jsonb default null
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare cards jsonb; current_settings jsonb; expected_config jsonb;
  related jsonb; item jsonb; settings_values jsonb; assignments text; affected bigint;
begin
  -- Same lock as booking settings/capacity; then row locks also catch ordinary
  -- page saves which do not acquire the advisory lock.
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text, 0));
  insert into tenant_config(tenant_id) values(p_tenant) on conflict do nothing;
  select setup_cards into cards from tenant_config where tenant_id=p_tenant for update;
  if not found then raise exception 'BLAM8_SETUP_UNAVAILABLE'; end if;
  if p_revision is null or p_revision < 0 or
     coalesce((cards->p_page->>'revision')::bigint,0) <> p_revision then
    raise exception 'BLAM8_SETUP_STALE: reload the card';
  end if;
  if p_page not in ('diary_settings','booking_details','dependants','invoice_settings','packages','crm_settings','crm_fields','knowledge') or p_page is null
     or jsonb_typeof(p_card) is distinct from 'object'
     or p_card->>'page' is distinct from p_page
     or (p_card->>'revision')::bigint is distinct from p_revision+1
     or jsonb_typeof(p_card->'lines') is distinct from 'array'
     or octet_length(p_card::text)>120000 then
    raise exception 'BLAM8_SETUP_INVALID';
  end if;
  if p_settings_patch is not null then
    if p_page='knowledge' then
      if jsonb_typeof(p_expected_settings->'sources') is distinct from 'array'
         or jsonb_typeof(p_settings_patch->'notes') is distinct from 'array'
         or jsonb_array_length(p_settings_patch->'notes') not between 1 and 20
         or exists(select 1 from jsonb_object_keys(p_settings_patch) k where k <> 'notes') then
        raise exception 'BLAM8_SETUP_INVALID';
      end if;
      -- Ordinary imports do not use the setup advisory lock. Protect the
      -- source collection, including a previously absent note, during CAS.
      lock table knowledge_chunks in share row exclusive mode;
      for item in select value from jsonb_array_elements(p_expected_settings->'sources') loop
        select coalesce(jsonb_agg(jsonb_build_object('id',id,'content',content) order by id),'[]')
          into related from knowledge_chunks
          where tenant_id=p_tenant and source=item->>'label' and mailbox_id is null;
        if related is distinct from item->'chunks' then
          raise exception 'BLAM8_SETTINGS_STALE: reload knowledge';
        end if;
      end loop;
      if exists(select 1 from jsonb_array_elements(p_settings_patch->'notes') n
        group by n->>'label' having count(*)>1) then raise exception 'BLAM8_SETUP_INVALID'; end if;
      for item in select value from jsonb_array_elements(p_settings_patch->'notes') loop
        if item->>'label' is null or item->>'content' is null
           or length(trim(item->>'label')) not between 1 and 80
           or length(trim(item->>'content')) not between 1 and 4000
           or jsonb_typeof(item->'chunks') is distinct from 'array'
           or jsonb_array_length(item->'chunks') not between 1 and 6
           or not exists(select 1 from jsonb_array_elements(p_expected_settings->'sources') s
             where s->>'label'=item->>'label')
           or exists(select 1 from jsonb_array_elements(item->'chunks') c
             where c->>'content' is null or c->>'embedding' is null) then
          raise exception 'BLAM8_SETUP_INVALID';
        end if;
        delete from knowledge_chunks where tenant_id=p_tenant and source=item->>'label' and mailbox_id is null;
        insert into knowledge_chunks(tenant_id,source,mailbox_id,content,embedding,source_chunk_index)
          select p_tenant,item->>'label',null,r.content,r.embedding,n
          from jsonb_array_elements(item->'chunks') with ordinality as chunks(c,n),
            lateral jsonb_populate_record(null::knowledge_chunks,c) r;
      end loop;
    elsif p_page='crm_fields' then
      if jsonb_typeof(p_expected_settings) is distinct from 'object'
         or jsonb_typeof(p_settings_patch) is distinct from 'object'
         or jsonb_typeof(p_settings_patch->'definitions') is distinct from 'array'
         or exists(select 1 from jsonb_object_keys(p_settings_patch) k where k <> 'definitions') then
        raise exception 'BLAM8_SETUP_INVALID';
      end if;
      lock table crm_field_definitions in share row exclusive mode;
      select coalesce(jsonb_agg(to_jsonb(d) order by d.id),'[]') into related
        from crm_field_definitions d where tenant_id=p_tenant and entity='contact';
      if related is distinct from p_expected_settings->'definitions' then
        raise exception 'BLAM8_SETTINGS_STALE: reload customer fields';
      end if;
      for item in select value from jsonb_array_elements(p_settings_patch->'definitions') loop
        if item->>'entity' is distinct from 'contact' then raise exception 'BLAM8_SETUP_INVALID'; end if;
        if exists(select 1 from crm_field_definitions where id=(item->>'id')::uuid and tenant_id=p_tenant and entity='contact') then
          if exists(select 1 from crm_field_definitions where id=(item->>'id')::uuid and tenant_id=p_tenant
                    and (key is distinct from item->>'key' or label is distinct from item->>'label'
                      or field_type is distinct from item->>'field_type' or durability is distinct from item->>'durability'
                      or ladder is not null)) then
            raise exception 'BLAM8_SETUP_INVALID: field identity and meaning must be preserved';
          end if;
          if exists(select 1 from crm_field_definitions where id=(item->>'id')::uuid and tenant_id=p_tenant
                    and field_type='dropdown' and not (options <@ (item->'options'))) then
            raise exception 'BLAM8_SETUP_INVALID: saved dropdown options must be preserved';
          end if;
          update crm_field_definitions set options=item->'options',required=(item->>'required')::boolean,
            extraction_hint=item->>'extraction_hint',probe_behaviour=item->>'probe_behaviour',
            recall_days=(item->>'recall_days')::int,valid_months=(item->>'valid_months')::int,
            staff_can_see=(item->>'staff_can_see')::boolean,
            staff_can_see_set=coalesce((item->>'staff_can_see_set')::boolean,staff_can_see_set)
            where id=(item->>'id')::uuid and tenant_id=p_tenant and entity='contact';
        else
          if (select count(*) from crm_field_definitions where tenant_id=p_tenant and entity='contact') >= 10 then
            raise exception 'BLAM8_SETUP_INVALID: customer field limit';
          end if;
          insert into crm_field_definitions(id,tenant_id,entity,key,label,field_type,options,required,
            extraction_hint,probe_behaviour,durability,recall_days,valid_months,display_order,staff_can_see,staff_can_see_set)
            values((item->>'id')::uuid,p_tenant,'contact',item->>'key',item->>'label',item->>'field_type',item->'options',
              (item->>'required')::boolean,item->>'extraction_hint',item->>'probe_behaviour',item->>'durability',
              (item->>'recall_days')::int,(item->>'valid_months')::int,(item->>'display_order')::int,
              (item->>'staff_can_see')::boolean,(item->>'staff_can_see_set')::boolean);
        end if;
      end loop;
    elsif p_page='crm_settings' then
      if jsonb_typeof(p_expected_settings) is distinct from 'object'
         or jsonb_typeof(p_settings_patch) is distinct from 'object'
         or jsonb_typeof(p_settings_patch->'stages') is distinct from 'array'
         or exists(select 1 from jsonb_object_keys(p_settings_patch) k where k not in ('stages','crm_quiet_days')) then
        raise exception 'BLAM8_SETUP_INVALID';
      end if;
      select to_jsonb(t.crm_quiet_days) into current_settings from tenant_config t where tenant_id=p_tenant;
      lock table crm_stages in share row exclusive mode;
      select coalesce(jsonb_agg(to_jsonb(s) order by s.id),'[]') into related from crm_stages s where tenant_id=p_tenant;
      if related is distinct from p_expected_settings->'stages'
         or coalesce(current_settings,'null'::jsonb) is distinct from p_expected_settings->'quiet_days' then
        raise exception 'BLAM8_SETTINGS_STALE: reload CRM settings';
      end if;
      if p_settings_patch ? 'crm_quiet_days' then
        if jsonb_typeof(p_settings_patch->'crm_quiet_days') is distinct from 'number'
           or (p_settings_patch->>'crm_quiet_days')::numeric not between 1 and 365
           or (p_settings_patch->>'crm_quiet_days')::numeric <> trunc((p_settings_patch->>'crm_quiet_days')::numeric) then
          raise exception 'BLAM8_SETUP_INVALID';
        end if;
        update tenant_config set crm_quiet_days=(p_settings_patch->>'crm_quiet_days')::int where tenant_id=p_tenant;
      end if;
      for item in select value from jsonb_array_elements(p_settings_patch->'stages') loop
        if item->>'stage_type' not in ('active','won','lost','dormant')
           or nullif(btrim(item->>'name'),'') is null then
          raise exception 'BLAM8_SETUP_INVALID';
        end if;
        if exists(select 1 from crm_stages where id=(item->>'id')::uuid and tenant_id=p_tenant) then
          if exists(select 1 from crm_stages where id=(item->>'id')::uuid and tenant_id=p_tenant
                    and (name is distinct from item->>'name' or stage_type is distinct from item->>'stage_type')) then
            raise exception 'BLAM8_SETUP_INVALID: existing stage identity and type must be preserved';
          end if;
          update crm_stages set display_order=(item->>'display_order')::int
            where id=(item->>'id')::uuid and tenant_id=p_tenant;
        else
          insert into crm_stages(id,tenant_id,name,stage_type,display_order)
            values((item->>'id')::uuid,p_tenant,item->>'name',item->>'stage_type',(item->>'display_order')::int);
        end if;
      end loop;
    elsif p_page in ('invoice_settings','packages') then
      if jsonb_typeof(p_expected_settings) is distinct from 'object'
         or jsonb_typeof(p_settings_patch) is distinct from 'object'
         or p_settings_patch = '{}'::jsonb
         or exists(select 1 from jsonb_object_keys(p_settings_patch) k where
           (p_page='packages' and k <> 'extras') or
           (p_page='invoice_settings' and k not in ('show_vat','vat_rate','show_business_details',
             'show_payment_terms','payment_terms','footer_note','default_quote_valid_days',
             'default_deposit_percent','extras'))) then
        raise exception 'BLAM8_SETUP_INVALID';
      end if;
      expected_config := p_expected_settings - '_setup_scheduling';
      if p_page='packages' and p_expected_settings ? '_setup_scheduling' then
        select to_jsonb(s) into related from scheduling_config s where tenant_id=p_tenant for update;
        if related is distinct from p_expected_settings->'_setup_scheduling' then
          raise exception 'BLAM8_SETTINGS_STALE: reload package services';
        end if;
      end if;
      select to_jsonb(i) into current_settings from invoice_settings i where tenant_id=p_tenant for update;
      if current_settings is null and expected_config = '{}'::jsonb then
        if not (p_settings_patch ?& array['show_vat','vat_rate','show_business_details',
                                        'show_payment_terms','payment_terms','default_quote_valid_days']) then
          raise exception 'BLAM8_SETUP_INVALID: explicit initial invoice choices required';
        end if;
        -- All active required choices were supplied above. Omitted optional
        -- choices remain unset; unrelated legacy columns retain their schema rules.
        settings_values := jsonb_build_object('extras','{}'::jsonb) || p_settings_patch ||
          jsonb_build_object('tenant_id',p_tenant,'updated_at',now());
        select string_agg(format('%I',k),',') into assignments from jsonb_object_keys(settings_values) k;
        execute 'insert into invoice_settings (' || assignments || ') select ' || assignments ||
          ' from jsonb_populate_record(null::invoice_settings,$1) on conflict do nothing' using settings_values;
        get diagnostics affected = row_count;
        if affected <> 1 then raise exception 'BLAM8_SETTINGS_STALE: reload invoice settings'; end if;
      elsif current_settings is distinct from expected_config then
        raise exception 'BLAM8_SETTINGS_STALE: reload invoice settings';
      else
        select string_agg(format('%1$I=source.%1$I',k),',') into assignments
          from jsonb_object_keys(p_settings_patch) k;
        execute 'update invoice_settings target set ' || assignments || ',updated_at=now()' ||
          ' from jsonb_populate_record(null::invoice_settings,$1) source where target.tenant_id=$2'
          using p_settings_patch,p_tenant;
      end if;
    else
    if p_expected_settings is null
       or jsonb_typeof(p_settings_patch) is distinct from 'object'
       or p_settings_patch = '{}'::jsonb
       or exists(select 1 from jsonb_object_keys(p_settings_patch) k
                 where (p_page='diary_settings' and k not in ('service_types','resource_types',
                   'capacity_per_window','working_days','open_time','close_time','timezone','booking_setting_review',
                   'slot_minutes','windows','buffer_minutes','min_notice_hours','horizon_days','blackout_dates',
                   'bank_holiday_nation','job_done_when','stay_pricing','day_hours','party_size','recurring',
                   'delivery','credits','courses','makeups','nouns','booking_mode','intake_fields'))
                    or (p_page='booking_details' and k not in ('intake_fields','definitions','booking_mode'))
                    or (p_page='dependants' and k not in ('groups','definitions'))) then
      raise exception 'BLAM8_SETUP_INVALID';
    end if;
    expected_config := case when p_page='diary_settings' then p_expected_settings - '_setup_stages' - '_setup_has_bookings'
                           else p_expected_settings->'config' end;
    if jsonb_typeof(expected_config) is distinct from 'object'
       or (p_page='booking_details' and not (p_settings_patch ?& array['intake_fields','definitions']))
       or (p_page='dependants' and not (p_settings_patch ? 'groups')) then
      raise exception 'BLAM8_SETUP_INVALID';
    end if;
    -- Protect whole related collections, including concurrent inserts which
    -- cannot be excluded by locking only the rows present in the preview.
    if p_page='diary_settings' and p_expected_settings ? '_setup_stages' then
      lock table crm_stages in share row exclusive mode;
      select coalesce(jsonb_agg(to_jsonb(s) order by s.id),'[]') into related
        from crm_stages s where tenant_id=p_tenant;
      if related is distinct from p_expected_settings->'_setup_stages' then
        raise exception 'BLAM8_SETTINGS_STALE: reload CRM stages';
      end if;
    elsif p_page='booking_details' then
      lock table crm_field_definitions in share row exclusive mode;
      select coalesce(jsonb_agg(to_jsonb(d) order by d.id),'[]') into related
        from crm_field_definitions d where tenant_id=p_tenant and entity='contact';
      if related is distinct from p_expected_settings->'definitions' then
        raise exception 'BLAM8_SETTINGS_STALE: reload customer fields';
      end if;
    elsif p_page='dependants' then
      if p_settings_patch ? 'definitions' then
        lock table crm_field_definitions in share row exclusive mode;
        select coalesce(jsonb_agg(to_jsonb(d) order by d.id),'[]') into related
          from crm_field_definitions d where tenant_id=p_tenant and entity='contact';
        if related is distinct from p_expected_settings->'definitions' then
          raise exception 'BLAM8_SETTINGS_STALE: reload progress definitions';
        end if;
      end if;
      lock table requirement_groups in share row exclusive mode;
      select coalesce(jsonb_agg(to_jsonb(g) order by g.id),'[]') into related
        from requirement_groups g where tenant_id=p_tenant;
      if related is distinct from p_expected_settings->'groups' then
        raise exception 'BLAM8_SETTINGS_STALE: reload subject kinds';
      end if;
    end if;
    select to_jsonb(c) into current_settings from scheduling_config c where tenant_id=p_tenant for update;
    if current_settings is null and expected_config = '{}'::jsonb then
      -- No-defaults migration makes a new row's undecided booking settings
      -- unset. A concurrent ordinary insert must still invalidate this preview.
      insert into scheduling_config(tenant_id) values(p_tenant) on conflict do nothing;
      if not found then raise exception 'BLAM8_SETTINGS_STALE: reload settings'; end if;
    elsif current_settings is distinct from expected_config then
      raise exception 'BLAM8_SETTINGS_STALE: reload settings';
    end if;
    if p_page in ('booking_details','diary_settings') and p_settings_patch ? 'booking_mode' then
      if p_settings_patch->>'booking_mode' is null
         or p_settings_patch->>'booking_mode' not in ('stay','appointment')
         or (p_page='booking_details' and jsonb_typeof(p_expected_settings->'has_bookings') is distinct from 'boolean')
         or (p_page='diary_settings' and jsonb_typeof(p_expected_settings->'_setup_has_bookings') is distinct from 'boolean') then
        raise exception 'BLAM8_SETUP_INVALID';
      end if;
      -- A mode transition can reinterpret even historical bookings. Exclude
      -- inserts from ordinary writers too, not only advisory-lock participants.
      if p_settings_patch->>'booking_mode' is distinct from coalesce(current_settings->>'booking_mode',
        case when exists(select 1 from jsonb_array_elements(coalesce(current_settings->'intake_fields','[]')) f
          where trim(regexp_replace(lower(f->>'label'),'[^a-z0-9]+',' ','g'))='departure collection date time')
          then 'stay' else 'appointment' end) then
        lock table bookings in share row exclusive mode;
        if exists(select 1 from bookings where tenant_id=p_tenant) then
          raise exception 'BLAM8_SETTINGS_STALE: booking history requires a mode migration';
        end if;
      end if;
    end if;
    if exists(select 1 from jsonb_each(p_settings_patch) e
      where e.key in ('service_types','resource_types','intake_fields','definitions','groups','blackout_dates')
        and jsonb_typeof(e.value) is distinct from 'array') then
      raise exception 'BLAM8_SETUP_INVALID';
    end if;
    settings_values := p_settings_patch - 'definitions' - 'groups';
    if p_page='booking_details' then
      settings_values := settings_values || jsonb_build_object('intake_configured_at',now());
    elsif p_page='dependants' then
      settings_values := settings_values || jsonb_build_object('dependants_configured_at',now());
    end if;
    -- Only the page's fixed allowlist reaches these quoted identifiers. Populate
    -- through the real table type and update all selected columns in one statement.
    select string_agg(format('%1$I=source.%1$I',k),',') into assignments
      from jsonb_object_keys(settings_values) k;
    execute 'update scheduling_config target set ' || assignments ||
      ',booking_revision=target.booking_revision+1,updated_at=now()' ||
      ' from jsonb_populate_record(null::scheduling_config,$1) source where target.tenant_id=$2'
      using settings_values,p_tenant;
    if p_page='booking_details' then
      for item in select value from jsonb_array_elements(p_settings_patch->'definitions') loop
        insert into crm_field_definitions(tenant_id,entity,key,label,field_type,options,required,
          extraction_hint,probe_behaviour,durability,recall_days,valid_months,display_order,staff_can_see)
        values(p_tenant,'contact',item->>'key',item->>'label',item->>'field_type',item->'options',
          (item->>'required')::boolean,item->>'extraction_hint',item->>'probe_behaviour',
          item->>'durability',(item->>'recall_days')::int,(item->>'valid_months')::int,
          (item->>'display_order')::int,(item->>'staff_can_see')::boolean);
      end loop;
    elsif p_page='dependants' then
      for item in select value from jsonb_array_elements(coalesce(p_settings_patch->'definitions','[]')) loop
        if item->>'entity' is distinct from 'contact' or item->>'field_type' is distinct from 'dropdown'
           or jsonb_typeof(item->'ladder') is distinct from 'object' then raise exception 'BLAM8_SETUP_INVALID'; end if;
        if exists(select 1 from crm_field_definitions where id=(item->>'id')::uuid and tenant_id=p_tenant and entity='contact') then
          if exists(select 1 from crm_field_definitions d where id=(item->>'id')::uuid and tenant_id=p_tenant
            and (ladder is null or key is distinct from item->>'key' or label is distinct from item->>'label'
              or exists(select 1 from jsonb_array_elements(d.options) with ordinality o(value,n)
                where o.value is distinct from (item->'options')->(o.n::int-1)))) then
            raise exception 'BLAM8_SETUP_INVALID: preserve progress identity and levels';
          end if;
          update crm_field_definitions set options=item->'options',ladder=item->'ladder',
            staff_can_see=(item->>'staff_can_see')::boolean,staff_can_see_set=(item->>'staff_can_see_set')::boolean
            where id=(item->>'id')::uuid and tenant_id=p_tenant and entity='contact';
        else
          if (select count(*) from crm_field_definitions where tenant_id=p_tenant and entity='contact') >= 10 then
            raise exception 'BLAM8_SETUP_INVALID: customer field limit';
          end if;
          insert into crm_field_definitions(id,tenant_id,entity,key,label,field_type,options,ladder,required,
            probe_behaviour,durability,recall_days,valid_months,display_order,staff_can_see,staff_can_see_set)
          values((item->>'id')::uuid,p_tenant,'contact',item->>'key',item->>'label','dropdown',item->'options',
            item->'ladder',false,'passive','once',30,0,(item->>'display_order')::int,
            (item->>'staff_can_see')::boolean,(item->>'staff_can_see_set')::boolean);
        end if;
      end loop;
      for item in select value from jsonb_array_elements(p_settings_patch->'groups') loop
        if exists(select 1 from requirement_groups where id=(item->>'id')::uuid and tenant_id=p_tenant) then
          if exists(select 1 from requirement_groups g, jsonb_array_elements(g.fields) old
            where g.id=(item->>'id')::uuid and g.tenant_id=p_tenant and jsonb_typeof(old->'ladder')='object'
              and not exists(select 1 from jsonb_array_elements(item->'fields') new
                where new->>'key'=old->>'key' and new->>'label'=old->>'label'
                  and new->>'type'=old->>'type' and jsonb_typeof(new->'ladder')='object'
                  and not exists(select 1 from jsonb_array_elements(old->'options') with ordinality o(value,n)
                    where o.value is distinct from (new->'options')->(o.n::int-1)))) then
            raise exception 'BLAM8_SETUP_INVALID: preserve subject progress identity and levels';
          end if;
          update requirement_groups set label=item->>'label',label_plural=item->>'label_plural',
            cardinality=item->>'cardinality',fields=item->'fields',updated_at=now()
            where id=(item->>'id')::uuid and tenant_id=p_tenant;
        else
          insert into requirement_groups(id,tenant_id,label,label_plural,cardinality,fields,display_order)
            values((item->>'id')::uuid,p_tenant,item->>'label',item->>'label_plural',
              item->>'cardinality',item->'fields',(item->>'display_order')::int);
        end if;
      end loop;
    end if;
    end if;
  elsif p_expected_settings is not null then
    raise exception 'BLAM8_SETUP_INVALID';
  end if;
  update tenant_config set setup_cards=jsonb_set(cards,array[p_page],p_card) where tenant_id=p_tenant;
  insert into setup_card_revisions(tenant_id,page,revision,card) values(p_tenant,p_page,p_revision+1,p_card);
  return p_card;
end $$;
revoke all on function change_setup_card(uuid,text,bigint,jsonb,jsonb,jsonb) from public;
commit;
