-- Phase 2: list/campaign policy around the existing CRM sequence engine.
begin;
alter table crm_sequences add column if not exists outreach_managed boolean not null default false;
alter table crm_sequence_enrollments add column if not exists outreach_once boolean not null default false;
create unique index if not exists outreach_enrollment_once on crm_sequence_enrollments(sequence_id,contact_id) where outreach_once;
create unique index if not exists crm_sequences_tenant_id_id on crm_sequences(tenant_id,id);
create unique index if not exists crm_contacts_tenant_id_id on crm_contacts(tenant_id,id);
create unique index if not exists email_connections_tenant_id_id on email_connections(tenant_id,id);
create unique index if not exists crm_enrollments_tenant_id_id on crm_sequence_enrollments(tenant_id,id);
create unique index if not exists email_log_tenant_id_id on email_log(tenant_id,id);

create table if not exists outreach_settings (
 tenant_id uuid primary key references tenants(id) on delete cascade,
 enabled boolean not null default false,
 daily_cap integer not null default 100 check(daily_cap between 1 and 1000),
 last_sync_at timestamptz,
 last_attempt_at timestamptz
);
create table if not exists outreach_lists (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 name text not null check(length(name) between 1 and 100),
 created_at timestamptz not null default now(), unique(tenant_id,id)
);
create table if not exists outreach_targets (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 list_id uuid not null, contact_id uuid not null, email text not null,
 basis text not null check(basis in ('permission','corporate')),
 evidence text not null check(length(evidence)>0),
 email_source text not null check(length(email_source)>0),
 company_number text not null default '',
 email_verified boolean not null default false, soft_match boolean not null default true,
 created_at timestamptz not null default now(),
 check(email=lower(trim(email))),
 check(basis<>'corporate' or length(company_number)>0),
 unique(tenant_id,list_id,email), unique(tenant_id,id),
 foreign key(tenant_id,list_id) references outreach_lists(tenant_id,id),
 foreign key(tenant_id,contact_id) references crm_contacts(tenant_id,id)
);
create table if not exists outreach_campaigns (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 list_id uuid not null, sequence_id uuid not null unique, mailbox_id uuid not null,
 name text not null, enabled boolean not null default false,
 config jsonb not null, last_result text not null default 'Paused',
 created_at timestamptz not null default now(), unique(tenant_id,id),
 foreign key(tenant_id,list_id) references outreach_lists(tenant_id,id),
 foreign key(tenant_id,sequence_id) references crm_sequences(tenant_id,id),
 foreign key(tenant_id,mailbox_id) references email_connections(tenant_id,id)
);
create table if not exists outreach_suppressions (
 tenant_id uuid not null references tenants(id) on delete cascade,
 email text not null check(email=lower(trim(email))), reason text not null,
 created_at timestamptz not null default now(), primary key(tenant_id,email)
);
create table if not exists outreach_messages (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 campaign_id uuid not null, enrollment_id uuid not null, target_id uuid not null,
 step_index integer not null, step_snapshot jsonb not null, config_snapshot jsonb not null,
 email text not null, mailbox_id uuid not null, sender_address text not null,
 status text not null default 'preparing' check(status in ('preparing','draft','sending','sent','unknown','cancelled')),
 work_token uuid, lease_until timestamptz, email_log_id uuid,
 created_at timestamptz not null default now(), attempted_at timestamptz, sent_at timestamptz,
 error text, unique(enrollment_id,step_index), unique(email_log_id),
 foreign key(tenant_id,campaign_id) references outreach_campaigns(tenant_id,id),
 foreign key(tenant_id,enrollment_id) references crm_sequence_enrollments(tenant_id,id),
 foreign key(tenant_id,target_id) references outreach_targets(tenant_id,id),
 foreign key(tenant_id,email_log_id) references email_log(tenant_id,id)
);
create index if not exists outreach_messages_capacity on outreach_messages(tenant_id,attempted_at);

-- Browser clients can inspect their business, but cannot forge reservations,
-- suppressions, verification evidence or campaign settings. App writes use the
-- service role after owner/manager, CRM entitlement and tenant checks.
do $$ declare t text; begin
 foreach t in array array['outreach_settings','outreach_lists','outreach_targets','outreach_campaigns','outreach_suppressions','outreach_messages'] loop
   execute format('alter table %I enable row level security',t);
   execute format('drop policy if exists outreach_tenant_read on %I',t);
   execute format('create policy outreach_tenant_read on %I for select to authenticated using (tenant_id in (select auth_tenant_ids()))',t);
   execute format('revoke all on %I from anon,authenticated',t);
   execute format('grant select on %I to authenticated',t);
   execute format('grant all on %I to service_role',t);
 end loop;
end $$;

create or replace function outreach_protect_sequence() returns trigger
language plpgsql set search_path=public as $$ begin
 if current_user not in ('postgres','service_role') and
    ((TG_OP='INSERT' and new.outreach_managed) or
     (TG_OP='UPDATE' and new.outreach_managed is distinct from old.outreach_managed)) then
   raise exception 'Outreach ownership is managed by the application';
 end if;
 return new;
end $$;
drop trigger if exists outreach_sequence_guard on crm_sequences;
create trigger outreach_sequence_guard before insert or update on crm_sequences
 for each row execute function outreach_protect_sequence();

create or replace function outreach_mark_enrollment() returns trigger
language plpgsql set search_path=public as $$
declare managed boolean;
begin
 -- Serialize ordinary enrollment with conversion to a managed campaign.
 select outreach_managed into managed from crm_sequences
 where id=new.sequence_id and tenant_id=new.tenant_id for share;
 if managed then
   new.outreach_once:=true;
 elsif TG_OP='UPDATE' and old.outreach_once then
   raise exception 'An outreach enrollment cannot be reassigned';
 end if;
 return new;
end $$;
drop trigger if exists outreach_enrollment_guard on crm_sequence_enrollments;
create trigger outreach_enrollment_guard before insert or update on crm_sequence_enrollments
 for each row execute function outreach_mark_enrollment();

-- Reserve composing work. Expired composing work is recoverable; an attempted
-- send is NEVER automatically retried. Cadence/advancement stays in crm_sequences.
create or replace function outreach_create_campaign(p_tenant uuid,p_name text,p_list uuid,p_sequence uuid,p_box uuid,p_config jsonb)
returns setof outreach_campaigns language plpgsql security invoker set search_path=public as $$
declare s crm_sequences;
begin
 select * into s from crm_sequences where id=p_sequence and tenant_id=p_tenant for update;
 if not found or s.outreach_managed or s.trigger_type<>'manual'
    or exists(select 1 from crm_sequence_enrollments where sequence_id=s.id)
    or (select count(*) from crm_sequence_steps where sequence_id=s.id and tenant_id=p_tenant) not between 1 and 7
    or exists(select 1 from crm_sequence_steps where sequence_id=s.id and tenant_id=p_tenant
      and (action_type<>'send_email' or not exit_on_reply or coalesce(trim(template_body),'')='' or coalesce(trim(template_subject),'')='')) then return; end if;
 update crm_sequences set outreach_managed=true where id=s.id;
 return query insert into outreach_campaigns(tenant_id,name,list_id,sequence_id,mailbox_id,config)
   values(p_tenant,p_name,p_list,p_sequence,p_box,p_config) returning *;
end $$;

create or replace function outreach_begin_step(p_tenant uuid,p_campaign uuid,p_enrollment uuid,
 p_target uuid,p_step integer,p_snapshot jsonb,p_config jsonb,p_email text,p_box uuid,p_sender text,p_token uuid)
returns setof outreach_messages language plpgsql security invoker set search_path=public as $$
begin
 insert into outreach_messages(tenant_id,campaign_id,enrollment_id,target_id,step_index,
   step_snapshot,config_snapshot,email,mailbox_id,sender_address,work_token,lease_until)
 values(p_tenant,p_campaign,p_enrollment,p_target,p_step,p_snapshot,p_config,p_email,p_box,p_sender,p_token,now()+interval '5 minutes')
 on conflict(enrollment_id,step_index) do nothing;
 update outreach_messages set work_token=p_token,lease_until=now()+interval '5 minutes',
   step_snapshot=p_snapshot,config_snapshot=p_config,email=p_email,mailbox_id=p_box,sender_address=p_sender
 where tenant_id=p_tenant and enrollment_id=p_enrollment and step_index=p_step
   and status='preparing' and (lease_until<now() or work_token=p_token);
 return query select * from outreach_messages
 where tenant_id=p_tenant and enrollment_id=p_enrollment and step_index=p_step;
end $$;

create or replace function outreach_finish_draft(p_tenant uuid,p_id uuid,p_token uuid,p_subject text,p_body text)
returns uuid language plpgsql security invoker set search_path=public as $$
declare m outreach_messages; log_id uuid;
begin
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'preparing' or m.work_token is distinct from p_token or m.lease_until<now() then return null; end if;
 log_id:=m.email_log_id;
 if log_id is null then
   insert into email_log(tenant_id,from_address,subject,body_in,draft_reply,status,kind,mailbox_id,source_message_id)
   values(p_tenant,m.email,p_subject,'[outreach sequence]',p_body,'pending','tenant_outreach',m.mailbox_id,'outreach:'||m.id::text)
   returning id into log_id;
 else
   update email_log set from_address=m.email,subject=p_subject,draft_reply=p_body,status='pending',mailbox_id=m.mailbox_id
   where id=log_id and tenant_id=p_tenant;
 end if;
 update outreach_messages set status='draft',email_log_id=log_id,work_token=null,lease_until=null,error=null where id=m.id;
 return log_id;
end $$;

-- One tenant row serializes ALL campaign sends, including manual approvals.
create or replace function outreach_claim(p_tenant uuid,p_id uuid,p_auto boolean)
returns text language plpgsql security invoker set search_path=public as $$
declare s outreach_settings; c outreach_campaigns; m outreach_messages;
 e crm_sequence_enrollments; t outreach_targets; p crm_contacts; b email_connections;
 step crm_sequence_steps; local_now timestamp; used integer; can_auto boolean;
begin
 select * into s from outreach_settings where tenant_id=p_tenant for update;
 if not found or not s.enabled then return 'Outreach is switched off'; end if;
 if not exists(select 1 from subscriptions where tenant_id=p_tenant and module='crm' and status='active') then return 'CRM is not active'; end if;
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'draft' then return 'This draft is not available to send'; end if;
 select * into c from outreach_campaigns where id=m.campaign_id and tenant_id=p_tenant;
 if not found or not c.enabled then return 'Campaign is paused'; end if;
 if c.config<>m.config_snapshot or c.mailbox_id<>m.mailbox_id then return 'Campaign changed; refresh this draft'; end if;
 if s.last_sync_at is null or s.last_sync_at<now()-interval '2 minutes' then return 'Reply sync is not current'; end if;
 select * into e from crm_sequence_enrollments where id=m.enrollment_id and tenant_id=p_tenant;
 if not found or e.status<>'active' or e.sequence_id<>c.sequence_id or e.current_step<>m.step_index
    or e.next_action_at>now() then return 'Enrollment is not due or active'; end if;
 if not exists(select 1 from crm_sequences where id=e.sequence_id and tenant_id=p_tenant and status='active' and outreach_managed) then return 'Sequence is paused'; end if;
 select * into step from crm_sequence_steps where tenant_id=p_tenant and sequence_id=e.sequence_id order by step_order limit 1 offset m.step_index;
 if not found or to_jsonb(step)<>m.step_snapshot or step.action_type<>'send_email' or not step.exit_on_reply then return 'Sequence changed; refresh this draft'; end if;
 select * into t from outreach_targets where id=m.target_id and tenant_id=p_tenant;
 if not found or t.list_id<>c.list_id or t.email<>m.email or not t.email_verified or t.soft_match
    or t.email_source='' or t.evidence='' or (t.basis='corporate' and t.company_number='') then return 'Recipient needs verification'; end if;
 select * into p from crm_contacts where id=e.contact_id and tenant_id=p_tenant;
 if not found or p.id<>t.contact_id or lower(trim(p.email)) is distinct from m.email or p.do_not_sequence then return 'Recipient is blocked or changed'; end if;
 if exists(select 1 from outreach_suppressions where tenant_id=p_tenant and email=m.email)
    or exists(select 1 from ignored_contacts where tenant_id=p_tenant and lower(trim(address))=m.email)
    or exists(select 1 from crm_contacts where tenant_id=p_tenant and lower(trim(email))=m.email and do_not_sequence) then return 'Recipient must not be contacted'; end if;
 if exists(select 1 from email_log where tenant_id=p_tenant and kind='inbound'
    and lower(trim(from_address))=m.email and created_at>=e.enrolled_at) then return 'Recipient replied'; end if;
 select * into b from email_connections where id=c.mailbox_id and tenant_id=p_tenant;
 if not found or b.sync_failing_since is not null or lower(trim(coalesce(nullif(b.send_as_address,''),b.address))) is distinct from m.sender_address then return 'Sending mailbox is unavailable or changed'; end if;
 if not exists(select 1 from email_log where id=m.email_log_id and tenant_id=p_tenant
    and kind='tenant_outreach' and status='pending' and from_address=m.email and mailbox_id=m.mailbox_id) then return 'Draft is no longer pending'; end if;
 if p_auto then
   select coalesce((select auto_send from mailbox_config where mailbox_id=c.mailbox_id and tenant_id=p_tenant),
                   (select auto_send from tenant_config where tenant_id=p_tenant),false) into can_auto;
   if c.config->>'mode'<>'auto' or not can_auto then return 'Approval required'; end if;
 end if;
 local_now:=now() at time zone (c.config->>'timezone');
 if not (c.config->'working_days') @> to_jsonb(extract(isodow from local_now)::integer-1)
    or (c.config->'holidays') @> to_jsonb(to_char(local_now,'YYYY-MM-DD'))
    or local_now::time<(c.config->>'start_time')::time or local_now::time>=(c.config->>'end_time')::time then return 'Outside working window'; end if;
 if exists(select 1 from outreach_messages where tenant_id=p_tenant and status in ('sending','unknown')) then return 'Reconcile an uncertain send first'; end if;
 if s.last_attempt_at>now()-make_interval(secs=>(c.config->>'min_gap_seconds')::integer) then return 'Minimum gap between emails'; end if;
 select count(*) into used from outreach_messages where tenant_id=p_tenant and attempted_at>now()-interval '24 hours';
 if used>=s.daily_cap then return 'Shared outreach cap reached'; end if;
 select count(*) into used from outreach_messages where tenant_id=p_tenant and campaign_id=c.id and attempted_at>now()-interval '24 hours';
 if used>=(c.config->>'daily_cap')::integer then return 'Campaign cap reached'; end if;
 if exists(select 1 from outreach_messages where tenant_id=p_tenant and email=m.email and attempted_at>now()-interval '24 hours') then return 'Recipient already contacted in the last 24 hours'; end if;
 update outreach_messages set status='sending',attempted_at=now(),error=null where id=m.id;
 update outreach_settings set last_attempt_at=now() where tenant_id=p_tenant;
 return 'claimed';
end $$;

-- Durable completion plus inbox status in ONE transaction. A failed response can
-- be recovered by reading this row; it never causes a second provider request.
create or replace function outreach_complete(p_tenant uuid,p_id uuid,p_body text,p_auto boolean)
returns void language plpgsql security invoker set search_path=public as $$
declare m outreach_messages;
begin
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'sending' then return; end if;
 update outreach_messages set status='sent',sent_at=now(),error=null where id=m.id;
 update email_log set status='sent',draft_reply=p_body,sent_at=now(),sent_via=case when p_auto then 'auto' else 'human' end
 where id=m.email_log_id and tenant_id=p_tenant;
end $$;
revoke all on function outreach_begin_step(uuid,uuid,uuid,uuid,integer,jsonb,jsonb,text,uuid,text,uuid),outreach_finish_draft(uuid,uuid,uuid,text,text),outreach_claim(uuid,uuid,boolean),outreach_complete(uuid,uuid,text,boolean) from public,anon,authenticated;
grant execute on function outreach_begin_step(uuid,uuid,uuid,uuid,integer,jsonb,jsonb,text,uuid,text,uuid),outreach_finish_draft(uuid,uuid,uuid,text,text),outreach_claim(uuid,uuid,boolean),outreach_complete(uuid,uuid,text,boolean) to service_role;
revoke all on function outreach_create_campaign(uuid,text,uuid,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function outreach_create_campaign(uuid,text,uuid,uuid,uuid,jsonb) to service_role;
commit;
