"""Process-local record versions: which CRM records changed since a cache
was built.

The portal caches the identified customer's on-file record for the session
(portal_data._profile_sections). Run #8 (2026-09-05): the owner moved the
customer's level on the CRM mid-session and the chat kept answering from
the stale record. Every contact write (db.crm_update_contact,
db.crm_merge_custom_data) bumps the record's version here; a cache that
remembers the version it was built at knows to rebuild. One uvicorn
process (render.yaml), so a dict is the whole story; a short TTL on the
cache covers a write from anywhere else.
"""
import threading

_lock = threading.Lock()
_versions: dict[tuple[str, str], int] = {}
_MAX_KEYS = 8192


def key(tenant_id, record_id) -> tuple[str, str]:
    return (str(tenant_id or ""), str(record_id or ""))


def get(tenant_id, record_id) -> int:
    """The record's current version (0 = never written in this process)."""
    with _lock:
        return _versions.get(key(tenant_id, record_id), 0)


def bump(tenant_id, record_id) -> int:
    """Mark the record as changed; returns the new version. Never raises."""
    k = key(tenant_id, record_id)
    with _lock:
        if len(_versions) >= _MAX_KEYS and k not in _versions:
            _versions.pop(next(iter(_versions)))
        _versions[k] = _versions.get(k, 0) + 1
        return _versions[k]
