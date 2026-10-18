-- Preserve messages, CRM receipts, calls and capability hashes.
-- Stop admission/dispatch at the application first; allow owned calls to drain
-- before revoking this RPC, otherwise carrier/model reconciliation is blocked.
begin;
update telephone_routes set enabled=false,forwarding_state=case when forwarding_state='restored' then 'restored' else 'restoration_required' end,updated_at=now();
revoke execute on function telephone_inbound_command(uuid,uuid,text,jsonb) from public,anon,authenticated,service_role;
revoke execute on function telephone_inbound_view(uuid,uuid) from public,anon,authenticated,service_role;
commit;
