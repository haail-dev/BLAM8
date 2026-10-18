-- Sign-off record for Concierge cards (R4b part 1, 16 Oct 2026). Additive and
-- idempotent: four nullable columns, no function, no policy change. Rows from before this
-- migration keep NULLs and are read exactly as they are today.
--   decided_at / decided_by / decided_via: when the card was settled by a person or the
--     clock, who (the signed-in user; NULL for expiry), and how:
--     'tap' | 'words' (a spoken or typed yes) | 'unknown' (route not recorded) | 'system' (expiry).
--     The outcome itself stays in status (executed, cancelled, dismissed, expired).
--   reprepared_from: the older card this one was prepared again from (part 2).
-- The app writes these with the service role after the outcome is settled, best effort;
-- existing RLS policies (tenant read, delegated rows service-only for writes) are unchanged.
begin;
alter table concierge_proposals add column if not exists decided_at timestamptz;
alter table concierge_proposals add column if not exists decided_by uuid;
alter table concierge_proposals add column if not exists decided_via text;
alter table concierge_proposals add column if not exists reprepared_from uuid;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'concierge_proposals_decided_via_check') then
    alter table concierge_proposals add constraint concierge_proposals_decided_via_check
      check (decided_via is null or decided_via in ('tap','words','unknown','system'));
  end if;
end $$;
commit;
