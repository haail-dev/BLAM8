drop function if exists public.claim_mailbox_import(uuid, uuid);
alter table public.email_connections drop column if exists backlog_import;
