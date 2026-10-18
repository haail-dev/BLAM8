begin;
create table if not exists public.knowledge_reviews (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  state text not null default 'pending' check(state in ('pending','saved','discarded')),
  payload jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists knowledge_reviews_tenant_created on public.knowledge_reviews(tenant_id,created_at,id);
alter table public.knowledge_reviews enable row level security;
revoke all on public.knowledge_reviews from anon, authenticated;
grant select, insert, update on public.knowledge_reviews to service_role;

create or replace function public.knowledge_review_context(p_tenant uuid)
returns jsonb language sql stable set search_path=public as $$
  with context as (select jsonb_build_object(
    'brief', coalesce((select business_brief from tenant_config where tenant_id=p_tenant),''),
    'sources', coalesce((select jsonb_agg(s order by s->>'source',s->>'mailbox_id') from
      (select jsonb_build_object('source',source,'mailbox_id',mailbox_id,'chunks',count(*),
        'content',string_agg(content,E'\n\n' order by created_at,source_chunk_index,id)) s
       from knowledge_chunks where tenant_id=p_tenant group by source,mailbox_id) sources),'[]'::jsonb),
    'reviews', coalesce((select jsonb_agg(to_jsonb(r) order by created_at,id)
      from knowledge_reviews r where tenant_id=p_tenant),'[]'::jsonb)) as data)
  select data || jsonb_build_object('version',md5(data::text)) from context;
$$;

create or replace function public.admit_knowledge_review(
  p_tenant uuid,p_review uuid,p_expected text,p_payload jsonb,p_notes jsonb,p_discard boolean default false)
returns boolean language plpgsql set search_path=public as $$
declare n jsonb; rows jsonb;
begin
  -- Match existing knowledge writers' lock order; the decision and all notes commit together.
  perform 1 from tenant_config where tenant_id=p_tenant for update;
  lock table knowledge_chunks in share row exclusive mode;
  lock table knowledge_reviews in share row exclusive mode;
  if not exists(select 1 from knowledge_reviews where id=p_review and tenant_id=p_tenant and state='pending')
    or knowledge_review_context(p_tenant)->>'version' is distinct from p_expected then
    raise exception 'BLAM8_KNOWLEDGE_REVIEW_STALE';
  end if;
  if p_discard and jsonb_array_length(p_notes)<>0 then raise exception 'BLAM8_SETUP_INVALID'; end if;
  for n in select value from jsonb_array_elements(p_notes) loop
    select jsonb_agg(c || jsonb_build_object('tenant_id',p_tenant,'source',n->>'label',
      'mailbox_id',n->'mailbox_id')) into rows from jsonb_array_elements(n->'chunks') c;
    perform replace_knowledge_source_atomic(p_tenant,n->>'label',(n->>'mailbox_id')::uuid,rows);
  end loop;
  update knowledge_reviews set state=case when p_discard then 'discarded' else 'saved' end,
    payload=p_payload where id=p_review and tenant_id=p_tenant;
  return true;
end $$;
revoke all on function public.knowledge_review_context(uuid) from public,anon,authenticated;
revoke all on function public.admit_knowledge_review(uuid,uuid,text,jsonb,jsonb,boolean) from public,anon,authenticated;
grant execute on function public.knowledge_review_context(uuid) to service_role;
grant execute on function public.admit_knowledge_review(uuid,uuid,text,jsonb,jsonb,boolean) to service_role;
commit;
