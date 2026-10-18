-- Revert application code first. Existing booking expiries are untouched.
alter table public.scheduling_config drop column if exists offer_last_call;
alter table public.scheduling_config drop column if exists offer_last_call_review;
