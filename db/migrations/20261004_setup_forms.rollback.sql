begin;
drop function if exists save_knowledge_form_checked(uuid,text,text,uuid,text,jsonb);
drop function if exists knowledge_form_snapshot(uuid,text,uuid);
drop function if exists save_setup_form_checked(uuid,text,jsonb,jsonb);
drop function if exists setup_form_snapshot(uuid,text);
commit;
