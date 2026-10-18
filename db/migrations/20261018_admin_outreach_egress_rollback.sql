-- Roll back the application first; it calls this read-only helper.
begin;
drop function if exists public.admin_outreach_stale_drafts(jsonb);
notify pgrst, 'reload schema';
commit;
