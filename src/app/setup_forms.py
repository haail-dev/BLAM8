"""Original form snapshots and atomic, tenant-scoped settings writes."""
import json
from uuid import uuid4
from fastapi import HTTPException
from app import db


def load(ctx, page):
    try:
        value = (ctx.client or db.db()).rpc('setup_form_snapshot', {
            'p_tenant': ctx.tenant_id, 'p_page': page}).execute().data
        if not isinstance(value, dict):
            raise ValueError('Missing snapshot')
        return parse(json.dumps(value), page)
    except Exception:
        raise HTTPException(503, 'The saved settings could not be verified. Reload before editing.') from None


def parse(raw, page):
    try:
        if not isinstance(raw, str) or len(raw) > 2000000:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('page') != page:
            raise ValueError()
        lists = {'crm_fields': ['definitions'], 'crm_settings': ['stages'],
                 'booking_details': ['definitions'],
                 'dependants': ['definitions', 'groups', 'instances']}
        if page not in lists:
            raise ValueError()
        for key in lists[page]:
            if not isinstance(value.get(key), list) or any(not isinstance(row, dict) for row in value[key]):
                raise ValueError()
        if page in ('booking_details', 'dependants') and not isinstance(value.get('config'), dict):
            raise ValueError()
        if page == 'crm_settings' and 'quiet_days' not in value:
            raise ValueError()
        for row in value.get('definitions', []):
            if not all(isinstance(row.get(k), str) and row[k] for k in ('id', 'entity', 'key')):
                raise ValueError()
        for row in value.get('groups', []) + value.get('instances', []):
            if not isinstance(row.get('id'), str) or not row['id']:
                raise ValueError()
        if any(not isinstance(row.get('group_id'), str) for row in value.get('instances', [])):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise HTTPException(409, 'This form has no valid saved version. Reload before saving.') from None


def save(ctx, page, expected, operations):
    try:
        (ctx.client or db.db()).rpc('save_setup_form_checked', {
            'p_tenant': ctx.tenant_id, 'p_page': page, 'p_expected': expected,
            'p_operations': operations}).execute()
    except Exception as exc:
        if 'BLAM8_SETTINGS_STALE: booking history requires migration' in str(exc):
            raise HTTPException(409, 'Changing booking mode with saved bookings requires a migration. This form saved nothing.') from None
        if 'BLAM8_SETTINGS_STALE' in str(exc):
            raise HTTPException(409, 'These settings changed after the form opened. Reload before saving; this form saved nothing.') from None
        raise HTTPException(503, 'The save could not be confirmed. Reload to check before trying again.') from None


def operation(table, fields=None, identity=None, delete=False):
    return {'table': table, 'op': 'delete' if delete else 'update' if identity else 'insert',
            'id': identity or str(uuid4()), 'fields': fields or {}}


def intake_plan(expected, fields, stay):
    from datetime import datetime, timezone
    from app import intake, intake_memory, crm_fields, setup_card_mode, setup_cards
    definitions = [dict(d) for d in expected['definitions'] if d['entity'] == 'contact']
    by_key = {d['key']: d for d in definitions}
    operations, rows, problems = [], [], []
    for field in intake.clean_fields(fields):
        f = dict(field)
        if f.get('remember'):
            key = f.get('crm_key') if f.get('crm_key') in by_key else crm_fields.slug(f['label'])
            if key not in by_key:
                try:
                    definition = crm_fields.prepare_field(intake_memory.definition_for(f), definitions)
                    op = operation('crm_field_definitions', definition)
                    operations.append(op)
                    definition = {**definition, 'id': op['id']}
                    by_key[key] = definition
                    definitions.append(definition)
                except crm_fields.FieldError as exc:
                    problems.append(f"{f['label']}: {exc}")
                    f = {k: v for k, v in f.items() if k not in ('remember', 'crm_key')}
            if f.get('remember'):
                f['crm_key'] = key
        rows.append(f)
    # SQL checks the actual booking history under lock. The pure adapter also
    # checks the capacity prerequisites for a mode change with no history.
    try:
        mode, _ = setup_card_mode.prepare({'id': 'form', 'fields': {'key': 'stay_mode', 'value': stay}},
            {'config': expected['config'], 'has_bookings': False})
    except setup_cards.Invalid as exc:
        raise HTTPException(400, str(exc)) from None
    mode['intake_fields'] = intake.apply_stay(rows, stay)
    if not stay and any(intake._norm(scope) == intake._norm(intake.STAYS_SCOPE)
                        for row in mode['intake_fields'] for scope in row.get('services', [])):
        raise HTTPException(400, 'Remove Stays-only field scopes before switching to appointments.')
    mode['intake_configured_at'] = datetime.now(timezone.utc).isoformat()
    return operations, mode, problems


def groups_plan(expected, groups, progress):
    from datetime import datetime, timezone
    from app import ladders, requirement_groups
    now = datetime.now(timezone.utc).isoformat()
    existing = {g['id']: g for g in expected['groups']}
    operations, saved = [], []
    for index, group in enumerate(groups):
        fields = {'label': group['label'], 'label_plural': group.get('label_plural') or '',
                  'cardinality': group.get('cardinality') or 'many', 'fields': group.get('fields') or [],
                  'display_order': index, 'updated_at': now}
        op = operation('requirement_groups', fields, group.get('id') if group.get('id') in existing else None)
        operations.append(op)
        saved.append({**fields, 'id': op['id']})
    kept = {g['id'] for g in saved}
    operations.extend(operation('requirement_groups', identity=gid, delete=True) for gid in existing if gid not in kept)
    wanted = {requirement_groups.key_for(l['label']): l for l in progress if l['who'] == ladders.CONTACT}
    definitions = [d for d in expected['definitions'] if d['entity'] == ladders.CONTACT]
    by_key = {d['key']: d for d in definitions}
    for key, lad in wanted.items():
        fields = {'label': lad['label'], 'field_type': 'dropdown', 'options': lad['levels'],
                  'ladder': {'notify': lad['notify'], 'gates': lad['gates']},
                  'extraction_hint': f"The customer's current {lad['label'].lower()} — one of: " + ', '.join(lad['levels'])}
        if key in by_key:
            operations.append(operation('crm_field_definitions', fields, by_key[key]['id']))
        else:
            operations.append(operation('crm_field_definitions', {'entity': ladders.CONTACT, 'key': key,
                'required': False, 'display_order': len(definitions) * 10, **fields}))
    operations.extend(operation('crm_field_definitions', {'ladder': None}, d['id'])
                      for key, d in by_key.items() if key not in wanted and isinstance(d.get('ladder'), dict))
    for group in saved:
        for instance in expected['instances']:
            if instance['group_id'] != group['id'] or instance.get('archived_at'):
                continue
            data = dict(instance.get('data') or {})
            changed = False
            for field in group['fields']:
                if not ladders.is_ladder(field):
                    continue
                value = data.get(field['key'])
                if value in (None, '') or value in ladders.levels_of(field):
                    continue
                normal = ladders.normalise_level(value, ladders.levels_of(field))
                if normal:
                    data[field['key']] = normal
                    changed = True
            if changed:
                operations.append(operation('requirement_instances', {'data': data}, instance['id']))
    return operations
