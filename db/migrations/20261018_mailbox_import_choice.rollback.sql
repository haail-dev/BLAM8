-- Fail closed on rollback: do not restore the old implicit claim permission.
create or replace function public.claim_mailbox_import(p_tenant uuid,p_connection uuid)
returns jsonb language sql set search_path=public as $$ select null::jsonb; $$;
drop function if exists public.choose_mailbox_import(uuid,uuid,uuid,text,text);
alter table public.email_connections drop column if exists backlog_import_choice;
