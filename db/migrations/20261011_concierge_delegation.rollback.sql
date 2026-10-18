-- Stop new delegation/confirmations and drain/inspect active revisions first.
-- Preserve task/history/receipts; old code must not resume revised/unknown work.
revoke execute on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) from authenticated,service_role;
-- Intentional additive columns/history retention: no destructive rollback.
