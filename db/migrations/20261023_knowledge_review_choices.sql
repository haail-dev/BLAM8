-- Persist draft decisions only. Active Knowledge still has one admission writer.
create or replace function public.checkpoint_knowledge_review(
  p_tenant uuid,p_review uuid,p_expected text,p_choices jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
begin
  perform 1 from public.tenant_config where tenant_id=p_tenant for update;
  lock table public.knowledge_chunks in share row exclusive mode;
  lock table public.knowledge_reviews in share row exclusive mode;
  if public.knowledge_review_context(p_tenant)->>'version' is distinct from p_expected then
    raise exception 'BLAM8_KNOWLEDGE_REVIEW_STALE';
  end if;
  if jsonb_typeof(p_choices) is distinct from 'object' or octet_length(p_choices::text)>350000 then
    raise exception 'BLAM8_SOURCE_INVALID';
  end if;
  update public.knowledge_reviews set payload=jsonb_set(payload,'{draft_review_choices}',p_choices)
    where id=p_review and tenant_id=p_tenant and state='pending';
  if not found then return null; end if;
  return public.knowledge_review_context(p_tenant);
end $$;
revoke all on function public.checkpoint_knowledge_review(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.checkpoint_knowledge_review(uuid,uuid,text,jsonb) to service_role;
