"""Knowledge form versions; content and its version are read together."""
import json
import re
from fastapi import HTTPException
from app import db

EMPTY_VERSION = 'd751713988987e9331980363e24189ce'


def read(ctx, source=None, mailbox=None):
    try:
        result = (ctx.client or db.db()).rpc('knowledge_form_snapshot', {
            'p_tenant': ctx.tenant_id, 'p_source': source, 'p_mailbox': mailbox}).execute().data
        if not isinstance(result, list):
            raise ValueError()
        return result
    except Exception:
        raise HTTPException(503, 'The saved knowledge could not be verified. Reload before editing.') from None


def version(raw, source, mailbox, *, existing=False):
    try:
        if not isinstance(raw, str) or len(raw) > 2000000:
            raise ValueError()
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError()
        seen, found = set(), None
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('source'), str):
                raise ValueError()
            if item.get('mailbox_id') is not None and not isinstance(item['mailbox_id'], str):
                raise ValueError()
            if not isinstance(item.get('version'), str) or not re.fullmatch('[0-9a-f]{32}', item['version']):
                raise ValueError()
            identity = (item['source'], item.get('mailbox_id'))
            if identity in seen:
                raise ValueError()
            seen.add(identity)
            if identity == (source, mailbox):
                found = item['version']
        if existing and found is None:
            raise ValueError()
        return found or EMPTY_VERSION
    except (TypeError, ValueError):
        raise HTTPException(409, 'This form has no valid saved version. Reload before saving.') from None


def save(tenant_id, source, new_source, mailbox, expected, rows, client=None):
    try:
        return (client or db.db()).rpc('save_knowledge_form_checked', {
            'p_tenant': tenant_id, 'p_source': source, 'p_new_source': new_source,
            'p_mailbox': mailbox, 'p_expected': expected, 'p_rows': rows}).execute().data
    except Exception as exc:
        if 'BLAM8_SETTINGS_STALE' in str(exc):
            raise HTTPException(409, 'This source changed or the new name is already in use. Reload before saving; this form saved nothing.') from None
        raise HTTPException(503, 'The save could not be confirmed. Reload to check before trying again.') from None
