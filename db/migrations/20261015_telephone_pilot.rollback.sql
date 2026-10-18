-- Reversible feature rollback: preserve call outcomes, receipts and dedupe keys.
-- Pending/unknown submissions are NOT retried or deleted. Reconcile under the
-- retained evidence before any later re-enable. Application feature also off.
begin;
update telephone_settings set enabled=false,updated_at=now();
revoke execute on function telephone_command(uuid,uuid,text,jsonb) from public,anon,authenticated,service_role;
commit;
