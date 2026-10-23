-- Additive metadata only. No tenant backfill and no operational settings changes.
-- Requires the existing change_setup_card function and its current migrations.
begin;
alter table tenant_config add column if not exists onboarding_state jsonb not null default '{}';
create table if not exists onboarding_revisions (
  tenant_id uuid not null references tenants(id) on delete cascade,
  revision bigint not null check(revision > 0),
  state jsonb not null,
  created_at timestamptz not null default now(),
  primary key(tenant_id, revision)
);
alter table onboarding_revisions enable row level security;
-- Sensitive answer history is read through authenticated application routes.
-- There is deliberately no ordinary client write grant or policy.
create or replace function save_onboarding_state(
  p_tenant uuid, p_revision bigint, p_state jsonb, p_card_write jsonb default null
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare current_state jsonb;
begin
  -- Same order as every existing setup writer to avoid lock inversion.
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text, 0));
  insert into tenant_config(tenant_id) values(p_tenant) on conflict do nothing;
  select onboarding_state into current_state from tenant_config where tenant_id=p_tenant for update;
  if not found then raise exception 'BLAM8_ONBOARDING_UNAVAILABLE'; end if;
  if p_revision is null or p_revision < 0
     or coalesce((current_state->>'revision')::bigint,0) <> p_revision then
    raise exception 'BLAM8_ONBOARDING_STALE';
  end if;
  if jsonb_typeof(p_state) is distinct from 'object'
     or p_state->>'version' is distinct from '1'
     or (p_state->>'revision')::bigint is distinct from p_revision+1
     or jsonb_typeof(p_state->'answers') is distinct from 'object'
     or jsonb_typeof(p_state->'reviews') is distinct from 'object'
     or jsonb_typeof(p_state->'proposals') is distinct from 'object'
     or octet_length(p_state::text)>500000 then
    raise exception 'BLAM8_ONBOARDING_INVALID';
  end if;
  if p_card_write is not null then
    if jsonb_typeof(p_card_write) is distinct from 'object' or
       p_card_write->>'page' not in ('diary_settings','invoice_settings','crm_settings') then
      raise exception 'BLAM8_ONBOARDING_INVALID';
    end if;
    perform change_setup_card(p_tenant,p_card_write->>'page',(p_card_write->>'revision')::bigint,
      p_card_write->'card',nullif(p_card_write->'expected_settings','null'::jsonb),
      nullif(p_card_write->'settings_patch','null'::jsonb));
  end if;
  update tenant_config set onboarding_state=p_state where tenant_id=p_tenant;
  insert into onboarding_revisions(tenant_id,revision,state) values(p_tenant,p_revision+1,p_state);
  return p_state;
end $$;
revoke all on function save_onboarding_state(uuid,bigint,jsonb,jsonb) from public;
do $$ declare r text; begin
  foreach r in array array['anon','authenticated'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on function save_onboarding_state(uuid,bigint,jsonb,jsonb) from %I',r);
      execute format('revoke all on onboarding_revisions from %I',r);
    end if;
  end loop;
  if exists(select 1 from pg_roles where rolname='service_role') then
    grant select,insert on onboarding_revisions to service_role;
    grant execute on function save_onboarding_state(uuid,bigint,jsonb,jsonb) to service_role;
  end if;
end $$;
commit;
