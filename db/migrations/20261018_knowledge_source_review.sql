-- Add source corrections to existing Knowledge admission; install after
-- 20261018_knowledge_review.sql. No feature is enabled by this migration.
begin;
create table if not exists public.knowledge_review_source_ids (
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  id uuid not null,
  primary key(tenant_id,id)
);
create table if not exists public.knowledge_review_sources (
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  id uuid not null,
  review_id uuid not null references public.knowledge_reviews(id) on delete cascade,
  evidence jsonb not null check(octet_length(evidence::text)<=400000),
  expires_at timestamptz not null,
  primary key(tenant_id,id)
);
alter table public.knowledge_review_source_ids enable row level security;
alter table public.knowledge_review_sources enable row level security;
revoke all on public.knowledge_review_source_ids from public,anon,authenticated,service_role;
revoke all on public.knowledge_review_sources from public,anon,authenticated,service_role;
grant select,insert,delete on public.knowledge_review_sources to service_role;

create or replace function public.reserve_knowledge_review_source_id()
returns trigger language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
begin
  if tg_op='UPDATE' then raise exception 'BLAM8_SOURCE_IMMUTABLE'; end if;
  insert into public.knowledge_review_source_ids(tenant_id,id) values(new.tenant_id,new.id);
  return new;
end $$;
revoke all on function public.reserve_knowledge_review_source_id() from public,anon,authenticated,service_role;
drop trigger if exists knowledge_review_source_immutable on public.knowledge_review_sources;
create trigger knowledge_review_source_immutable before insert or update on public.knowledge_review_sources
  for each row execute function public.reserve_knowledge_review_source_id();

create or replace function public.capture_knowledge_review_sources(
  p_tenant uuid,p_review uuid,p_payload jsonb,p_evidence jsonb)
returns uuid language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
begin
  perform 1 from public.tenant_config where tenant_id=p_tenant for update;
  if p_payload->>'source_snapshot' is distinct from p_evidence->>'id'
     or jsonb_typeof(p_evidence->'pages') is distinct from 'array'
     or jsonb_array_length(p_evidence->'pages') not between 1 and 8
     or (p_evidence->>'expires_at') is null
     or (p_evidence->>'expires_at')::timestamptz > now()+interval '30 days 1 minute'
     or (p_evidence->>'expires_at')::timestamptz <= now()
     or exists(select 1 from jsonb_array_elements(p_evidence->'pages') p
       where coalesce(length(p->>'text'),0) not between 1 and 12000)
     or (select sum(length(p->>'text')) from jsonb_array_elements(p_evidence->'pages') p)>60000 then
    raise exception 'BLAM8_SOURCE_INVALID';
  end if;
  if (select count(*) from public.knowledge_review_sources where tenant_id=p_tenant and expires_at>now())>=10 then
    raise exception 'BLAM8_SOURCE_LIMIT';
  end if;
  insert into public.knowledge_reviews(id,tenant_id,payload) values(p_review,p_tenant,p_payload);
  insert into public.knowledge_review_sources(tenant_id,id,review_id,evidence,expires_at)
    values(p_tenant,(p_evidence->>'id')::uuid,p_review,p_evidence,(p_evidence->>'expires_at')::timestamptz);
  return p_review;
end $$;

create or replace function public.revise_knowledge_review_sources(
  p_tenant uuid,p_review uuid,p_expected text,p_payload jsonb)
returns boolean language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
declare previous jsonb;
begin
  -- Same lock order as final admission; corrections cannot race a save.
  perform 1 from public.tenant_config where tenant_id=p_tenant for update;
  lock table public.knowledge_chunks in share row exclusive mode;
  lock table public.knowledge_reviews in share row exclusive mode;
  select payload into previous from public.knowledge_reviews
    where id=p_review and tenant_id=p_tenant and state='pending';
  if previous is null or public.knowledge_review_context(p_tenant)->>'version' is distinct from p_expected then
    raise exception 'BLAM8_KNOWLEDGE_REVIEW_STALE';
  end if;
  if p_payload->>'source_snapshot' is distinct from previous->>'source_snapshot'
     or p_payload->'source_units' is distinct from previous->'source_units'
     or (p_payload->>'draft_revision')::int is distinct from coalesce((previous->>'draft_revision')::int,1)+1
     or not exists(select 1 from public.knowledge_review_sources
       where tenant_id=p_tenant and review_id=p_review and id=(previous->>'source_snapshot')::uuid and expires_at>now()) then
    raise exception 'BLAM8_SOURCE_INVALID';
  end if;
  update public.knowledge_reviews set payload=p_payload where id=p_review and tenant_id=p_tenant;
  return true;
end $$;
revoke all on function public.capture_knowledge_review_sources(uuid,uuid,jsonb,jsonb) from public,anon,authenticated;
revoke all on function public.revise_knowledge_review_sources(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.capture_knowledge_review_sources(uuid,uuid,jsonb,jsonb) to service_role;
grant execute on function public.revise_knowledge_review_sources(uuid,uuid,text,jsonb) to service_role;
commit;
