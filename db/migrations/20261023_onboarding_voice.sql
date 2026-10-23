-- Existing valid configuration stays valid. This adds an atomic Voice form save.
create or replace function public.save_onboarding_voice(
  p_tenant uuid,p_expected jsonb,p_values jsonb,p_required text[])
returns boolean language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
declare previous public.tenant_config; changed public.tenant_config; actual jsonb; field text;
begin
  perform 1 from public.tenants where id=p_tenant for update;
  if not found then raise exception 'BLAM8_VOICE_STALE'; end if;
  insert into public.tenant_config(tenant_id) values(p_tenant) on conflict(tenant_id) do nothing;
  select * into previous from public.tenant_config where tenant_id=p_tenant for update;
  select jsonb_object_agg(key,coalesce(to_jsonb(previous)->key,'null'::jsonb)) into actual
    from jsonb_object_keys(p_expected) key;
  if actual is distinct from p_expected then raise exception 'BLAM8_VOICE_STALE'; end if;
  if jsonb_typeof(p_values) is distinct from 'object' or p_values-'business_brief'-'agent_name'-'tone'-'sign_off'-'voice_notes'-'voice_profile_source'-'voice_learning_status'-'examples'<>'{}'::jsonb then
    raise exception 'BLAM8_VOICE_INVALID';
  end if;
  foreach field in array p_required loop
    if jsonb_typeof(p_values->field) is distinct from 'string' or btrim(p_values->>field,E' \t\r\n')='' then
      raise exception 'BLAM8_VOICE_INVALID';
    end if;
  end loop;
  changed:=jsonb_populate_record(previous,p_values);
  update public.tenant_config set business_brief=changed.business_brief,
    agent_name=changed.agent_name,tone=changed.tone,sign_off=changed.sign_off,voice_notes=changed.voice_notes,
    voice_profile_source=changed.voice_profile_source,voice_learning_status=changed.voice_learning_status,
    examples=case when btrim(coalesce(previous.examples,''))='' then changed.examples else previous.examples end
    where tenant_id=p_tenant;
  return true;
end $$;
revoke all on function public.save_onboarding_voice(uuid,jsonb,jsonb,text[]) from public,anon,authenticated;
grant execute on function public.save_onboarding_voice(uuid,jsonb,jsonb,text[]) to service_role;
