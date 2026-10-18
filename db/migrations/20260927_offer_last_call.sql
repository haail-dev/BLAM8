-- Astra: optional last call, with no default for new businesses.
do $$
begin
    if exists (select 1 from information_schema.columns
               where table_schema='public' and table_name='scheduling_config'
                 and column_name='offer_last_call') then
        return;
    end if;
    alter table public.scheduling_config add column offer_last_call time;
    alter table public.scheduling_config add column offer_last_call_review text;

    update public.scheduling_config
    set offer_last_call = time '00:00' - make_interval(hours => min_notice_hours),
        offer_last_call_review = 'carried_over_review'
    where min_notice_hours > 0 and min_notice_hours < 24;
end $$;
