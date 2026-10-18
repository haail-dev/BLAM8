begin;
-- Preserve approvals, versions, gathered evidence and unresolved tasks.
revoke execute on function telephone_objective_command(uuid,uuid,text,jsonb) from public,anon,authenticated,service_role;
commit;
