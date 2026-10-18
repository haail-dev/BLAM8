-- Preserve proposals and receipts for a forward reapplication. Disable only
-- the new entry point; older application code ignores the additive storage.
-- Retain the additive invoice days column and any explicitly saved values too.
-- Retain knowledge chunk positions so reapplication cannot lose paragraph order.
begin;
drop function if exists change_setup_card(uuid,text,bigint,jsonb,jsonb,jsonb);
drop function if exists replace_knowledge_source_atomic(uuid,text,uuid,jsonb);
drop function if exists save_invoice_settings_checked(uuid,jsonb,jsonb,text);
commit;
