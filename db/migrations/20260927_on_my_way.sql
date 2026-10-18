-- Run with both On-my-way routes stopped; old workers cannot honour receipts.
-- No business settings are chosen by this migration.
begin;
create unique index if not exists bookings_on_my_way_tenant_id on public.bookings(tenant_id,id);
create table if not exists public.on_my_way_actions (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  booking_id uuid not null,
  action_key text not null,
  state text not null check (state in ('preparing','pre_send_failure','unknown','sent')),
  attempt uuid not null,
  lease_until timestamptz,
  eta text,
  person_name text,
  initiator jsonb not null default '{}',
  sheet_id uuid,
  receipt jsonb not null default '{}',
  history jsonb not null default '[]',
  notice_id uuid,
  notice_state text check (notice_state in ('unknown','attempted')),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (tenant_id, booking_id, action_key),
  foreign key (tenant_id,booking_id) references public.bookings(tenant_id,id)
);
alter table public.on_my_way_actions enable row level security;
-- Only the server may claim or certify delivery. No browser write policy.
revoke all on public.on_my_way_actions from public, anon, authenticated;
grant select, insert, update on public.on_my_way_actions to service_role;

-- Historical timestamps are not proof of delivery. Keep the evidence and hold.
insert into public.on_my_way_actions
  (tenant_id, booking_id, action_key, state, attempt, receipt)
select b.tenant_id, b.id, 'initial', 'unknown', gen_random_uuid(),
  jsonb_build_object('reason','historical_evidence_requires_review',
    'historical_sheet_ids',(select jsonb_agg(s.id) from public.job_sheets s
      where s.tenant_id=b.tenant_id and s.booking_id=b.id and s.on_my_way_at is not null),
    'historical_log_ids',(select jsonb_agg(e.id) from public.email_log e
      where e.tenant_id=b.tenant_id and e.kind='on_my_way' and b.booking_ref is not null
        and position('Booking reference: ' || b.booking_ref || '.' in e.body_in)>0))
from public.bookings b
where exists (select 1 from public.job_sheets s where s.tenant_id=b.tenant_id
  and s.booking_id=b.id and s.on_my_way_at is not null)
or exists (select 1 from public.email_log e where e.tenant_id=b.tenant_id
  and e.kind='on_my_way' and b.booking_ref is not null
  and position('Booking reference: ' || b.booking_ref || '.' in e.body_in)>0)
on conflict (tenant_id,booking_id,action_key) do nothing;

create or replace function public.on_my_way_action(
  p_tenant uuid, p_booking uuid, p_key text, p_attempt uuid,
  p_operation text, p_data jsonb default '{}', p_sheet uuid default null
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare a on_my_way_actions; previous on_my_way_actions; b bookings; notice_body text;
begin
  -- A booking lock serialises both routes, corrections and dispatch rechecks.
  select * into b from bookings where id=p_booking and tenant_id=p_tenant for update;
  if not found then return jsonb_build_object('error','notlive'); end if;
  if p_operation in ('claim','dispatch','owner_resend') then
    if b.status not in ('confirmed','proposed') then
      return jsonb_build_object('error','notlive');
    end if;
    if p_sheet is not null then
      perform 1 from job_sheets where id=p_sheet and tenant_id=p_tenant
        and booking_id=p_booking and closed_at is null
        and b.booking_date + coalesce((b.start_block+b.length_blocks)/96,0)
          >= (clock_timestamp() at time zone 'Europe/London')::date
        for update;
      if not found then return jsonb_build_object('error','notlive'); end if;
    end if;
  end if;
  select * into a from on_my_way_actions
    where tenant_id=p_tenant and booking_id=p_booking and action_key=p_key for update;
  if p_operation='owner_resend' then
    if p_sheet is not null then return jsonb_build_object('error','notlive'); end if;
    select * into previous from on_my_way_actions
      where tenant_id=p_tenant and booking_id=p_booking order by created_at desc limit 1;
    if a.id is null or a.id is distinct from previous.id then
      return jsonb_build_object('error','stale_action');
    end if;
    -- The form identifies the uncertain ATTEMPT, not merely its action key.
    -- Replayed forms must not release a later uncertain send.
    if a.state <> 'unknown' or a.attempt::text is distinct from p_data->>'resend_attempt' then
      return to_jsonb(a) || jsonb_build_object('owned',false);
    end if;
    perform reconcile_on_my_way(p_tenant,p_booking,a.id,false,
      jsonb_build_object('reason','owner chose to resend','initiator',p_data->'initiator',
        'previous_attempt',a.attempt,'eta',p_data->>'eta')::text);
    select * into a from on_my_way_actions where id=a.id;
    p_operation := 'claim';
  end if;
  if p_operation='claim' then
    if a.id is null then
      if p_key <> 'initial' then
        select * into previous from on_my_way_actions
          where tenant_id=p_tenant and booking_id=p_booking order by created_at desc limit 1;
        if previous.id is null or previous.state <> 'sent'
          or p_key <> 'after:' || previous.id::text then
          return jsonb_build_object('error','stale_action');
        end if;
      end if;
      insert into on_my_way_actions(tenant_id,booking_id,action_key,state,attempt,
        lease_until,eta,person_name,sheet_id,initiator)
      values(p_tenant,p_booking,p_key,'preparing',p_attempt,
        clock_timestamp()+interval '5 minutes',p_data->>'eta',p_data->>'person_name',p_sheet,
        coalesce(p_data->'initiator','{}'))
      returning * into a;
    elsif a.state='pre_send_failure' or
      (a.state='preparing' and a.lease_until < clock_timestamp()) then
      update on_my_way_actions set state='preparing',attempt=p_attempt,
        history=history || jsonb_build_array(to_jsonb(a)-'history'), receipt='{}',
        -- Historical evidence has no chosen ETA. Only after definite
        -- non-acceptance may the current explicit form supply the missing facts.
        eta=case when p_data ? 'resend_attempt' then p_data->>'eta' else coalesce(eta,p_data->>'eta') end,
        person_name=case when eta is null or p_data ? 'resend_attempt' then p_data->>'person_name' else person_name end,
        initiator=case when eta is null or p_data ? 'resend_attempt' then coalesce(p_data->'initiator','{}') else initiator end,
        lease_until=clock_timestamp()+interval '5 minutes',updated_at=clock_timestamp()
        where id=a.id returning * into a;
    else
      return to_jsonb(a) || jsonb_build_object('owned',false);
    end if;
    return to_jsonb(a) || jsonb_build_object('owned',true);
  end if;
  if a.id is null then return jsonb_build_object('error','unavailable'); end if;
  if p_operation='notice' then
    if a.state not in ('sent','unknown','pre_send_failure') then
      return jsonb_build_object('owned',false);
    end if;
    notice_body := left(coalesce(p_data->>'what','') || ' · ' || case a.state
      when 'sent' then 'the customer''s been told'
      when 'pre_send_failure' then 'customer message was not sent — retry after checking the details'
      else 'customer delivery is unknown — check before sending again' end,500);
    if a.notice_id is not null then
      update concierge_notifications set body=notice_body
        where id=a.notice_id and tenant_id=p_tenant;
      return to_jsonb(a) || jsonb_build_object('owned',false);
    end if;
    -- The owner's in-app notice and its claim commit together. Push delivery
    -- remains best effort; uncertainty cannot remove or duplicate this record.
    insert into concierge_notifications(tenant_id,kind,title,body,url,contact)
      values(p_tenant,'team_on_way',left(p_data->>'title',200),notice_body,
        p_data->>'url',nullif(lower(trim(p_data->>'contact')),'')) returning id into a.notice_id;
    update on_my_way_actions set notice_state='unknown',notice_id=a.notice_id
      where id=a.id returning * into a;
    return to_jsonb(a) || jsonb_build_object('owned',true,'notice_body',notice_body);
  elsif p_operation='notice_attempted' then
    update on_my_way_actions set notice_state='attempted' where id=a.id returning * into a;
  elsif a.attempt <> p_attempt then
    return jsonb_build_object('error','stale_attempt');
  elsif p_operation='dispatch' and a.state='preparing' and a.lease_until >= clock_timestamp() then
    if (p_data->>'booking_updated_at')::timestamptz is distinct from b.updated_at then
      return jsonb_build_object('error','stale_action');
    end if;
    update on_my_way_actions set state='unknown',receipt=p_data,updated_at=clock_timestamp()
      where id=a.id returning * into a;
  elsif p_operation='fail' and a.state='preparing' then
    update on_my_way_actions set state='pre_send_failure',receipt=p_data,updated_at=clock_timestamp()
      where id=a.id returning * into a;
  elsif p_operation='not_sent' and a.state='unknown' then
    -- Only a typed provider-wrapper rejection raised before its HTTP call.
    -- A timeout or arbitrary exception must never use this transition.
    update on_my_way_actions set state='pre_send_failure',receipt=receipt || p_data,
      updated_at=clock_timestamp() where id=a.id returning * into a;
  elsif p_operation='sent' and a.state='unknown' then
    update on_my_way_actions set state='sent',receipt=receipt || p_data,updated_at=clock_timestamp()
      where id=a.id returning * into a;
  else
    return jsonb_build_object('error','stale_attempt');
  end if;
  return to_jsonb(a) || jsonb_build_object('owned',true);
end $$;
revoke all on function public.on_my_way_action(uuid,uuid,text,uuid,text,jsonb,uuid) from public, anon, authenticated;
grant execute on function public.on_my_way_action(uuid,uuid,text,uuid,text,jsonb,uuid) to service_role;

-- An operator uses this only with provider/account evidence, never on a timer.
create or replace function public.reconcile_on_my_way(
  p_tenant uuid, p_booking uuid, p_action uuid, p_sent boolean, p_evidence text
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare a on_my_way_actions;
begin
  if p_sent is null or length(trim(coalesce(p_evidence,''))) < 10 then
    raise exception 'Provider evidence and a definite outcome are required';
  end if;
  perform 1 from bookings where id=p_booking and tenant_id=p_tenant for update;
  if not found then return null; end if;
  update on_my_way_actions set
    state=case when p_sent then 'sent' else 'pre_send_failure' end,
    receipt=receipt || jsonb_build_object('reconciliation_evidence',p_evidence,
      'reconciled_at',clock_timestamp(),'reconciled_sent',p_sent),
    updated_at=clock_timestamp()
    where id=p_action and tenant_id=p_tenant and booking_id=p_booking and state='unknown'
    returning * into a;
  return to_jsonb(a);
end $$;
revoke all on function public.reconcile_on_my_way(uuid,uuid,uuid,boolean,text) from public, anon, authenticated;
grant execute on function public.reconcile_on_my_way(uuid,uuid,uuid,boolean,text) to service_role;
commit;
