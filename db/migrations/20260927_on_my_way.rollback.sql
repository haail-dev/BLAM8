-- Stop BOTH On-my-way routes before rollback. Do not deploy the old unguarded
-- sender. Preserve all claims/receipts, including uncertain delivery evidence.
begin;
revoke execute on function public.on_my_way_action(uuid,uuid,text,uuid,text,jsonb,uuid) from service_role;
revoke execute on function public.reconcile_on_my_way(uuid,uuid,uuid,boolean,text) from service_role;
revoke insert, update on public.on_my_way_actions from service_role;
commit;
