-- Founder outreach only. Apply before enabling the server job; no tenant policies.
begin;
alter table vcrm_prospects add column if not exists company_number text;
alter table vcrm_prospects add column if not exists email_source text;
alter table vcrm_prospects add column if not exists email_verified boolean not null default false;
alter table vcrm_prospects add column if not exists soft_match boolean not null default true;

create table if not exists admin_outreach_config (
  id integer primary key check (id=1),
  enabled boolean not null default false,
  config jsonb not null default '{}'::jsonb,
  lease_token uuid,
  lease_until timestamptz,
  last_attempt_at timestamptz,
  last_sync_at timestamptz,
  last_result jsonb not null default '{}'::jsonb
);
insert into admin_outreach_config(id) values(1) on conflict do nothing;
create table if not exists admin_outreach_suppressions (
  email text primary key check (email=lower(trim(email))),
  reason text not null,
  created_at timestamptz not null default now()
);
create table if not exists admin_outreach_messages (
  id uuid primary key default gen_random_uuid(),
  prospect_id uuid references vcrm_prospects(id) on delete set null,
  email text not null,
  step integer not null check (step in (0,1)),
  status text not null check (status in ('draft','approved','sending','sent','unknown','cancelled')),
  payload jsonb not null default '{}'::jsonb,
  config_snapshot jsonb not null default '{}'::jsonb,
  provider_id text,
  error text,
  created_at timestamptz not null default now(),
  attempted_at timestamptz,
  sent_at timestamptz,
  unique(email,step)
);
create table if not exists admin_outreach_events (
  id text primary key,
  email text not null,
  kind text not null,
  created_at timestamptz not null default now()
);
create table if not exists admin_outreach_digests (
  day date primary key,
  summary jsonb not null,
  updated_at timestamptz not null default now()
);
alter table admin_outreach_config enable row level security;
alter table admin_outreach_suppressions enable row level security;
alter table admin_outreach_messages enable row level security;
alter table admin_outreach_events enable row level security;
alter table admin_outreach_digests enable row level security;
revoke all on admin_outreach_config,admin_outreach_suppressions,admin_outreach_messages,
  admin_outreach_events,admin_outreach_digests from anon,authenticated;
grant all on admin_outreach_config,admin_outreach_suppressions,admin_outreach_messages,
  admin_outreach_events,admin_outreach_digests to service_role;

-- Preserve legacy touches, including duplicate prospect rows sharing a mailbox.
insert into admin_outreach_messages(prospect_id,email,step,status,sent_at,attempted_at,provider_id)
select distinct on (lower(trim(p.email)), case when c.summary like 'Follow-up sent%' then 1 else 0 end)
 p.id,lower(trim(p.email)),case when c.summary like 'Follow-up sent%' then 1 else 0 end,
 'sent',c.created_at,c.created_at,(regexp_match(c.summary,'Resend id ([A-Za-z0-9-]+)'))[1]
from vcrm_calls c join vcrm_prospects p on p.id=c.prospect_id
where p.email is not null and trim(p.email)<>'' and
 (c.summary like 'Emailed %' or c.summary like 'Follow-up sent%' or c.summary like 'SENT%' or c.summary like 'Confirmed%')
order by lower(trim(p.email)),case when c.summary like 'Follow-up sent%' then 1 else 0 end,c.created_at
on conflict(email,step) do nothing;
insert into admin_outreach_suppressions(email,reason)
select distinct on (lower(trim(email))) lower(trim(email)),coalesce(next_action,'Legacy do-not-contact')
from vcrm_prospects where email is not null and trim(email)<>'' and
 (status='NOT_NOW' or coalesce(next_action,'') ~* '(STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact)')
on conflict do nothing;

create or replace function admin_outreach_lock(p_token uuid) returns boolean
language plpgsql security invoker set search_path=public as $$
begin
 update admin_outreach_config set lease_token=p_token,lease_until=now()+interval '5 minutes'
 where id=1 and (lease_until is null or lease_until<now());
 return found;
end $$;

-- Serialize ALL workers at the irreversible send boundary. A reservation is never
-- retried automatically, even after a restart or an ambiguous provider timeout.
create or replace function admin_outreach_claim(p_id uuid,p_token uuid,p_config jsonb,
 p_observed integer,p_provider_ids text[]) returns boolean
language plpgsql security invoker set search_path=public as $$
declare cfg admin_outreach_config; msg admin_outreach_messages; p vcrm_prospects;
 local_now timestamp; used integer; planned integer; completed integer; plan_start date;
begin
 select * into cfg from admin_outreach_config where id=1 for update;
 if not cfg.enabled or cfg.config<>p_config or cfg.lease_token is distinct from p_token or cfg.lease_until<now()
    or cfg.last_sync_at is null or cfg.last_sync_at<now()-interval '2 minutes' then return false; end if;
 local_now := now() at time zone (p_config->>'timezone');
 if not (p_config->'working_days') @> to_jsonb((extract(isodow from local_now)::integer-1))
    or (p_config->'holidays') @> to_jsonb(to_char(local_now,'YYYY-MM-DD'))
    or local_now::time<(p_config->>'start_time')::time or local_now::time>=(p_config->>'end_time')::time
    or cfg.last_attempt_at>now()-make_interval(secs=>(p_config->>'min_gap_seconds')::integer) then return false; end if;
 select * into msg from admin_outreach_messages where id=p_id for update;
 if not found or msg.config_snapshot<>p_config or
    not (msg.status='approved' or (msg.status='draft' and p_config->>'mode'='auto')) then return false; end if;
 if exists(select 1 from admin_outreach_suppressions where email=msg.email) then return false; end if;
 if exists(select 1 from vcrm_prospects where lower(trim(email))=msg.email and status in ('NOT_NOW','REPLIED','CALL_BOOKED','SIGNED_UP')) then return false; end if;
 if exists(select 1 from admin_outreach_events where email=msg.email and kind='replied') then return false; end if;
 if exists(select 1 from admin_outreach_messages where status in ('sending','unknown')) then return false; end if;
 select * into p from vcrm_prospects where id=msg.prospect_id;
 if not found or lower(trim(p.email)) is distinct from msg.email or p.kind is distinct from (p_config->>'kind') or
    not p.email_verified or p.soft_match or coalesce(p.company_number,'')='' or coalesce(p.email_source,'')='' or
    coalesce(p.next_action,'') ~* '(STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact)' then return false; end if;
 if (msg.step=0 and p.status is distinct from 'IDENTIFIED') or (msg.step=1 and
    (p.status is distinct from 'EMAILED' or p.next_action_date is null or p.next_action_date>local_now::date)) then return false; end if;
 if msg.step=1 and not exists(select 1 from admin_outreach_messages where email=msg.email and step=0 and status='sent') then return false; end if;
 if msg.step=0 then
   select coalesce(sum(value::integer),0),min(key::date) into planned,plan_start
     from jsonb_each_text(p_config->'plan') where key::date<=local_now::date;
   select count(*) into completed from admin_outreach_messages where step=0 and status='sent'
     and (sent_at at time zone (p_config->>'timezone'))::date>=plan_start;
   if completed>=planned then return false; end if;
 end if;
 -- Rolling 24h is deliberately stricter than a provider's midnight reset.
 select count(*) into used from admin_outreach_messages where attempted_at>now()-interval '24 hours'
   and (provider_id is null or not(provider_id=any(p_provider_ids)));
 if p_observed+used >= (p_config->>'daily_cap')::integer then return false; end if;
 update admin_outreach_messages set status='sending',attempted_at=now() where id=p_id;
 update admin_outreach_config set last_attempt_at=now() where id=1;
 return true;
end $$;

create or replace function admin_outreach_complete(p_id uuid,p_provider_id text,p_due date)
returns void language plpgsql security invoker set search_path=public as $$
declare msg admin_outreach_messages;
begin
 select * into msg from admin_outreach_messages where id=p_id for update;
 if msg.status not in ('sending','unknown') then return; end if;
 update admin_outreach_messages set status='sent',provider_id=p_provider_id,sent_at=now() where id=p_id;
 update vcrm_prospects set status=case when msg.step=0 then 'EMAILED' else status end,
   next_action=case when msg.step=0 then 'One short follow-up, then stop' else 'Follow-up sent. No further email.' end,
   next_action_date=case when msg.step=0 then p_due else null end,updated_at=now()
 where id=msg.prospect_id and status in ('IDENTIFIED','EMAILED')
   and not exists(select 1 from admin_outreach_suppressions where email=msg.email);
 if msg.prospect_id is not null then
 insert into vcrm_calls(prospect_id,summary) values(msg.prospect_id,
   case when msg.step=0 then 'Emailed via Resend' else 'Follow-up sent' end || ' (server outreach). Resend id ' || p_provider_id);
 end if;
end $$;
revoke all on function admin_outreach_lock(uuid) from public,anon,authenticated;
revoke all on function admin_outreach_claim(uuid,uuid,jsonb,integer,text[]) from public,anon,authenticated;
revoke all on function admin_outreach_complete(uuid,text,date) from public,anon,authenticated;
grant execute on function admin_outreach_lock(uuid) to service_role;
grant execute on function admin_outreach_claim(uuid,uuid,jsonb,integer,text[]) to service_role;
grant execute on function admin_outreach_complete(uuid,text,date) to service_role;
commit;
