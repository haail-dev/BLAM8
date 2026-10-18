-- Preserve occurrence history: never re-execute uncertain actions.
begin;
update schedule_settings set enabled=false;
update schedule_items set status='paused' where status='active';
commit;
