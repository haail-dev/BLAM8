-- Pause first, retain evidence, suppressions and durable send reservations.
begin;
update outreach_settings set enabled=false;
update outreach_campaigns set enabled=false,last_result='Paused for rollback';
-- Keep the sequence guard and the send ledger. Do not clear outreach_managed
-- or restart these enrollments through an older, unguarded sequence worker.
-- Before reverting application code, pause all managed sequence enrollments:
update crm_sequence_enrollments set status='paused'
 where sequence_id in (select id from crm_sequences where outreach_managed);
commit;
