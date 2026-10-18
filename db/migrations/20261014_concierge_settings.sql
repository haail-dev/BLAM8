-- Per-business Concierge settings (task nudge / expire / stall minutes, and later ones).
-- Additive: one jsonb column, empty by default, so every business keeps the server defaults.
alter table tenants add column if not exists concierge_settings jsonb not null default '{}'::jsonb;
