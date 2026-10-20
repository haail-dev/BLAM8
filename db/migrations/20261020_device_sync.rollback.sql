begin;
drop function if exists public.finish_device_sync(uuid,uuid,uuid,jsonb,jsonb);
drop function if exists public.claim_device_sync(uuid,uuid,text,uuid,text,bigint);
drop table if exists public.device_sync_operations;
drop table if exists public.device_sync;
commit;
