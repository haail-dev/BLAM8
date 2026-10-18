-- Safe rollback: the app falls back to the server defaults when the column is absent.
alter table tenants drop column if exists concierge_settings;
