-- Disable KNOWLEDGE_SOURCE_REVIEW_ENABLED first. This removes raw retained
-- sources and their ID reservations, but keeps pending/approved admission notes.
begin;
drop function if exists public.revise_knowledge_review_sources(uuid,uuid,text,jsonb);
drop function if exists public.capture_knowledge_review_sources(uuid,uuid,jsonb,jsonb);
drop table if exists public.knowledge_review_sources;
drop function if exists public.reserve_knowledge_review_source_id();
drop table if exists public.knowledge_review_source_ids;
commit;
