-- Disable only the new write API. Keep metadata and answer history intact.
-- Deploy the prior app first; old routes/settings/cards remain compatible.
begin;
drop function if exists save_onboarding_state(uuid,bigint,jsonb,jsonb);
commit;
