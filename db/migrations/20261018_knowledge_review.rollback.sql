-- Export review provenance before rolling back; knowledge chunks are preserved.
drop function if exists public.admit_knowledge_review(uuid,uuid,text,jsonb,jsonb,boolean);
drop function if exists public.knowledge_review_context(uuid);
drop table if exists public.knowledge_reviews;
