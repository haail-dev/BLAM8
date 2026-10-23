-- Existing Voice values and completion remain intact.
drop function if exists public.save_onboarding_voice(uuid,jsonb,jsonb,text[]);
