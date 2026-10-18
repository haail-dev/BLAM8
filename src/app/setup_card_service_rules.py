"""Explicit service exceptions and completion tasks, preserving other rules."""
from copy import deepcopy
from app import services, after_job, setup_card_scalars as scalars

FIELDS = {
    'service_urgent': {'name', 'enabled', 'same_day_until', 'notice_hours'},
    'service_out_of_hours': {'name', 'enabled', 'until', 'days', 'bank_holidays', 'price', 'price_after', 'offer', 'price_basis', 'terms'},
    'service_after_job': {'name', 'tasks'},
}


def legacy(name, raw):
    from app import setup_rows
    parts = [p.strip() for p in raw.split('|')]
    count = {'service_urgent': 3, 'service_out_of_hours': 6, 'service_after_job': 2}[name]
    if len(parts) != count: raise ValueError('The service rule contains missing or extra parts.')
    if name == 'service_urgent':
        return dict(name=parts[0], enabled=True, same_day_until=parts[1], notice_hours=parts[2])
    if name == 'service_after_job':
        return dict(name=parts[0], tasks=[s.strip() for s in parts[1].split(';') if s.strip()])
    if parts[3].lower() not in {'yes', 'no', 'true', 'false', '1', '0'}:
        raise ValueError('Choose bank-holiday availability explicitly.')
    return dict(name=parts[0], enabled=True, until=parts[1] or None, days=setup_rows.day_codes(parts[2]),
                bank_holidays=parts[3].lower() in {'yes', 'true', '1'}, price=parts[4], price_after=parts[5] or None)


def value(raw, kind, **kw):
    spec = dict(type=kind, nullable=False, minimum=0, maximum=1000000, choices=None) | kw
    return scalars.parse({'key': 'value', 'value': raw}, {'value': spec})['value']


def parse(name, raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS[name]:
        raise ValueError('Supply supported named service rules.')
    out = deepcopy(raw)
    if not isinstance(out.get('name'), str) or not out['name'].strip() or len(out['name']) > 60:
        raise ValueError('Supply the exact service name within sixty characters.')
    out['name'] = out['name'].strip()
    if out.get('enabled') is False:
        return {'name': out['name'], 'enabled': False}
    for key in ('enabled', 'bank_holidays'):
        if key in out: out[key] = value(out[key], 'boolean')
    for key in ('same_day_until', 'until'):
        if key in out: out[key] = value(out[key], 'time', nullable=key == 'until')
    if 'notice_hours' in out: out['notice_hours'] = value(out['notice_hours'], 'number', maximum=72)
    for key in ('price', 'price_after'):
        if key in out: out[key] = value(out[key], 'money', minimum=0.01, nullable=key == 'price_after')
    for key, choices in [('offer', services.OOH_OFFERS), ('price_basis', ['first_hour', 'booking'])]:
        if key in out: out[key] = value(out[key], 'choice', choices=choices)
    if 'days' in out:
        if not isinstance(out['days'], list) or any(d not in services.DAY_KEYS for d in out['days']) or len(set(out['days'])) != len(out['days']):
            raise ValueError('Choose distinct extra weekdays.')
        if len(out['days']) == 7:
            raise ValueError('All seven extra days cannot be represented by the current out-of-hours rules.')
    if 'terms' in out and (not isinstance(out['terms'], str) or len(out['terms']) > 1000):
        raise ValueError('Supply terms within one thousand characters, or explicit empty text.')
    if 'tasks' in out:
        tasks = out['tasks']
        if not isinstance(tasks, list) or len(tasks) > after_job.MAX_TASKS or any(not isinstance(t, str) or len(t) > 120 for t in tasks):
            raise ValueError('Supply up to five completion tasks with explicit deadlines.')
        parsed = after_job.clean_tasks(tasks)
        if len(parsed) != len(tasks): raise ValueError('Every task needs a distinct title and one to 400 calendar or working days.')
        for text, task in zip(tasks, parsed):
            match = after_job._LINE.fullmatch(' '.join(text.split()))
            if not match or match['title'] != task['title']:
                raise ValueError('Task titles must fit within eighty characters without losing text.')
    return out


def named(name, service):
    result = {'name': service['name']}
    if name == 'service_urgent':
        result['enabled'] = bool(service.get('same_day_until'))
        if result['enabled']:
            result.update(same_day_until=service['same_day_until'])
            if service.get('notice_hours') is not None: result['notice_hours'] = service['notice_hours']
    elif name == 'service_out_of_hours':
        ooh = service.get('out_of_hours')
        result['enabled'] = bool(ooh)
        if ooh:
            result.update(until=ooh.get('until'), days=[services.DAY_KEYS[d] for d in ooh.get('days', [])],
                          bank_holidays=bool(ooh.get('bank_holidays')), price=ooh.get('price'), price_after=ooh.get('price_after'),
                          offer=ooh.get('offer'), price_basis=ooh.get('price_basis'), terms=ooh.get('terms', ''))
    else:
        result['tasks'] = [after_job.as_text([t]) for t in service.get('after_job') or []]
    return result


def patch(selected, cfg):
    from app.setup_cards import Invalid
    rows = deepcopy((cfg or {}).get('service_types') or [])
    changes = []
    for line in selected:
        name = line['name']
        try: supplied = parse(name, line['fields'])
        except (ValueError, TypeError) as exc: raise Invalid(str(exc)) from None
        matches = [r for r in rows if services._norm(r['name']) == services._norm(supplied['name'])]
        if len(matches) != 1: raise Invalid('Choose one saved service, or accept its service row in the same batch.')
        service = matches[0]
        before = deepcopy(service)
        fields = named(name, service) | supplied
        if name != 'service_after_job' and not fields.get('enabled') and supplied.get('enabled') is not False:
            raise Invalid('Choose enabled=true or enabled=false explicitly for this service rule.')
        if supplied.get('enabled') is False: fields = {'name': service['name'], 'enabled': False}
        required = FIELDS[name] if fields.get('enabled', True) else {'name', 'enabled'}
        if not required <= fields.keys(): raise Invalid('Choose every active service rule explicitly: ' + ', '.join(sorted(required - fields.keys())) + '.')
        try: fields = parse(name, fields)
        except (ValueError, TypeError) as exc: raise Invalid(str(exc)) from None
        if name == 'service_urgent':
            for key in ('same_day_until', 'notice_hours'):
                if fields['enabled']: service[key] = fields[key]
                else: service.pop(key, None)
        elif name == 'service_out_of_hours':
            if not fields['enabled']: service.pop('out_of_hours', None)
            else:
                block = {k: v for k, v in fields.items() if k not in {'name', 'enabled'}}
                block['days'] = [services.DAY_KEYS.index(d) for d in fields['days']]
                effective = services.clean_out_of_hours(block)
                if effective is None: raise Invalid('Out-of-hours service needs explicit availability and a price.')
                review = (before.get('out_of_hours') or {}).get('carried_over_review', [])
                if any(k not in supplied for k in review): raise Invalid('Confirm the carried-over price basis and terms explicitly.')
                # Preserve unknown attributes, while removing explicitly cleared known values.
                updated = deepcopy(before.get('out_of_hours') or {})
                for key in block: updated.pop(key, None)
                updated.update(effective)
                updated.pop('carried_over_review', None)
                service['out_of_hours'] = updated
        elif 'tasks' in supplied:
            if fields['tasks'] and cfg.get('job_done_when') not in {'marked', 'day_end'}:
                raise Invalid('Choose when a job counts as done before adding its completion deadlines.')
            existing = {t['title'].lower(): t for t in service.get('after_job') or []}
            service['after_job'] = [deepcopy(existing.get(t['title'].lower(), {})) | t for t in after_job.clean_tasks(fields['tasks'])]
        changes.append({'id': line['id'], 'name': name, 'fields': named(name, service),
                        'before': before, 'after': deepcopy(service), 'review_before': named(name, before),
                        'review_after': named(name, service), 'notes': [
                            'Other service rules are preserved. Existing bookings and completion tasks are not rewritten.']})
    return {'service_types': rows}, changes
