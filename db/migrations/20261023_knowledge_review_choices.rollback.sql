-- Rollback leaves retained draft choices in the payload for audit/recovery.
drop function if exists public.checkpoint_knowledge_review(uuid,uuid,text,jsonb);
