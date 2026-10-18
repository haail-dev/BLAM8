-- Rollback for 20261016_concierge_sign_off.sql. Like the delegation rollback it RETAINS
-- the columns and every stamp already written (the record of who approved what must not be
-- destroyed by a rollback); it only drops the value check, so older code keeps working and
-- the forward migration can be applied again.
begin;
alter table concierge_proposals drop constraint if exists concierge_proposals_decided_via_check;
commit;
