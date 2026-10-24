"""One read-only business picture shared by the Records and Diary setup steps.

Built before a step asks anything (reports/diary-discovery-spec-2026-10-24.md,
Part 0). Deterministic: no model call and no writes. Every fact is a signal with
a stable id, a trust tier and its source, so the assistant can cite facts but
never supply them. The industry label is never evidence, pending Knowledge is
never read, and a booking setting the owner has not confirmed is a gap, not a
value (docs/agnostic-onboarding.md).
"""
from __future__ import annotations

from hashlib import sha256
import json

# Strongest first. Prefills need an owner-confirmed or saved source; a
# suggestion may also rest on what the owner submitted in the interview.
TRUST = ('confirmed', 'owner_submitted', 'config', 'owner_saved_source', 'derived', 'held')
PREFILL_TRUST = frozenset({'confirmed', 'config'})
SUGGEST_TRUST = frozenset({'confirmed', 'owner_submitted', 'config'})
CONFIRMED_PROVENANCE = frozenset({'owner_answer', 'about_you_owner_review', 'knowledge_owner_review',
                                  'owner_accepted_proposal', 'owner_confirmed_prefill'})
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
STEPS = ('records', 'diary')
MODEL_CAP = 16_000
SOURCE_TEXTS, SOURCE_CHARS, LIST_CAP = 12, 600, 40


class DbRead:
    """The database reads beyond onboarding_state.load. Tests pass a fake."""

    def __init__(self, tenant_id, client=None):
        self.tenant_id, self.client = tenant_id, client

    def team_people(self):
        from app import db
        return db.list_team_people(self.tenant_id)

    def field_definitions(self):
        from app import db
        return db.crm_list_field_definitions(self.tenant_id, client=self.client)

    def stages(self):
        from app import db
        return db.crm_list_stages(self.tenant_id, client=self.client)

    def source_texts(self):
        from app import db
        return db.knowledge_source_texts(self.tenant_id, client=self.client)

    def count(self, table):
        from app import db
        return db.count_tenant_rows(table, self.tenant_id)


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _text(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def _signal(kind, key, trust, text, value=None, source=None, rank=1):
    # Rank orders signals of one kind without making their ids order-dependent.
    return dict(id=f'{kind}:{key}', kind=kind, trust=trust, text=_text(text, 600),
                value=value, source=source or {}, rank=rank)


class _Pack:
    def __init__(self):
        self.signals, self.date_sources, self.gaps = [], [], []

    def add(self, *args, **kwargs):
        self.signals.append(_signal(*args, **kwargs))

    def guarded(self, gap, read):
        """A failed read is a gap; the step still runs with what was read."""
        try:
            return read()
        except Exception:
            self.gaps.append(gap)
            return None


def build(context, *, read=None):
    """The shared picture for `context` from onboarding_state.load(knowledge=True)."""
    from app import booking_policy, diary_config, onboarding_state, services
    read = read or DbRead(context['tenant_id'], context.get('client'))
    state, config, cfg = context['state'], context.get('config') or {}, context.get('cfg') or {}
    pack = _Pack()

    # 1. Answers the owner already gave or confirmed.
    for key, answer in sorted(state.get('answers', {}).items()):
        if answer.get('state') != 'answered' or answer.get('value') in (None, ''):
            continue
        provenance = (answer.get('provenance') or {}).get('kind', 'owner_answer')
        if provenance not in CONFIRMED_PROVENANCE:
            continue
        pack.add('answer', key, 'confirmed', f"{answer.get('question')}: {answer.get('value')}",
                 dict(question=answer.get('question'), scope_type=answer.get('scope_type'),
                      scope_id=answer.get('scope_id'), value=answer.get('value')),
                 dict(type='onboarding_answer', reference=key))

    # 2. Saved, current Knowledge answers only; pending drafts never appear.
    if context.get('knowledge_unavailable'):
        pack.gaps.append('knowledge_unavailable')
    candidates = pack.guarded('knowledge_unavailable',
                              lambda: onboarding_state.knowledge_candidates(context.get('knowledge'))) or []
    for item in candidates:
        if item['state'] != 'answered' or not item.get('answer'):
            continue
        pack.add('knowledge_answer', item['id'].split('.', 1)[1], 'confirmed',
                 f"{item['text']} {item['answer']}",
                 dict(topic=item['topic'], question=item['text'], answer=item['answer']),
                 dict(type='knowledge_review', reference=item['source_review']))

    # 3. What the owner submitted in the interview. The industry is display only.
    for field in ('business_brief', 'goals'):
        if _text(config.get(field), 2000):
            pack.add('business', field, 'owner_submitted', config[field], dict(field=field),
                     dict(type='interview', reference=field))

    # 4. Saved Knowledge wording, owner clarifications first.
    texts = pack.guarded('source_texts_unavailable', read.source_texts) or []
    texts = sorted(texts, key=lambda t: (not str(t.get('source', '')).startswith('Owner clarifications'),
                                         str(t.get('source', ''))))
    for item in texts[:SOURCE_TEXTS]:
        clarification = str(item.get('source', '')).startswith('Owner clarifications')
        pack.add('source_text', _hash(item.get('source'))[:12], 'owner_saved_source',
                 item.get('text'), dict(source=_text(item.get('source'), 120)),
                 dict(type='knowledge_source', reference=_text(item.get('source'), 120)),
                 rank=0 if clarification else 1)

    # 5–7. Saved Diary configuration, trusted only once the owner confirmed it.
    for item in services.clean_service_types(cfg.get('service_types'))[:LIST_CAP]:
        pack.add('service', _hash(item['name'].lower())[:12], 'config', item['name'],
                 dict(name=item['name'], minutes=item.get('minutes'), resource=item.get('resource'),
                      timing=item.get('timing')),
                 dict(type='scheduling_config', reference='service_types'))
    profile = booking_policy.compile_profile(cfg)
    hours = {}
    for setting in booking_policy.FIELDS:
        value = getattr(profile, setting)
        if value.valid and value.state == 'set' and value.review_status == 'confirmed':
            hours[setting] = value.values
            pack.add('booking_setting', setting, 'config', f"{setting}: {', '.join(map(str, value.values))}",
                     dict(setting=setting, values=list(value.values)),
                     dict(type='scheduling_config', reference=setting))
        elif value.state != 'off':
            pack.gaps.append(f'unconfirmed_{setting}')
    if cfg.get('booking_mode'):
        pack.add('booking_setting', 'booking_mode', 'config', f"booking_mode: {cfg['booking_mode']}",
                 dict(setting='booking_mode', values=[cfg['booking_mode']]),
                 dict(type='scheduling_config', reference='booking_mode'))
    for item in diary_config.clean_resource_types(cfg.get('resource_types'))[:LIST_CAP]:
        pack.add('resource', _hash(item['name'].lower())[:12], 'config', f"{item['name']} ({item['count']})",
                 dict(name=item['name'], count=item['count']),
                 dict(type='scheduling_config', reference='resource_types'))

    # 8. People: count and names only. Team assignment is People settings' job.
    people = [p for p in (pack.guarded('people_unavailable', read.team_people) or []) if p.get('active', True)]
    if people:
        pack.add('people', 'team', 'config', f"{len(people)} people: "
                 + ', '.join(_text(p.get('name'), 60) for p in people[:20]),
                 dict(count=len(people)), dict(type='team_people', reference='active'))

    # Section decisions the owner already made (needed, off or deferred).
    for capability, choice in sorted(state.get('capabilities', {}).items()):
        if choice in ('needed', 'off', 'deferred'):
            pack.add('capability', capability, 'confirmed', f'{capability}: {choice}',
                     dict(capability=capability, choice=choice), dict(type='onboarding_capability', reference=capability))

    # 9. Modules decide availability, never meaning.
    for module in sorted(context.get('modules') or ()):
        pack.add('module', module, 'config', module, dict(module=module), dict(type='subscription'))

    # 10 and 10b. Records already set up, including the dates a diary can follow.
    for group in context.get('groups') or []:
        group_label = _text(group.get('label') or group.get('name'), 60)
        if group_label:
            pack.add('record', f"group:{group.get('id')}", 'config', f"Related record: {group_label}",
                     dict(record='requirement_group', id=str(group.get('id')), label=group_label,
                          plural=_text(group.get('plural'), 60) or None, cardinality=group.get('cardinality'),
                          fields=[dict(key=f.get('key'), label=_text(f.get('label'), 60), type=f.get('type'),
                                       durability=f.get('durability')) for f in (group.get('fields') or [])[:LIST_CAP]]),
                     dict(type='requirement_groups', reference=str(group.get('id'))))
        for field in group.get('fields') or []:
            if field.get('durability') == 'expires':
                pack.date_sources.append(dict(
                    id=f"requirement_group:{group.get('id')}:{field.get('key')}",
                    label=_text(f"{group_label} {field.get('label')}".strip(), 80),
                    record='requirement_group', field=field.get('key'), durability='expires'))
    for field in pack.guarded('fields_unavailable', read.field_definitions) or []:
        label = _text(field.get('label'), 60)
        pack.add('record', f"field:{field.get('entity')}:{field.get('key')}", 'config',
                 f"{field.get('entity')} detail: {label}",
                 dict(record='crm_field', entity=field.get('entity'), key=field.get('key'),
                      type=field.get('field_type')),
                 dict(type='crm_field_definitions', reference=str(field.get('id'))))
        if field.get('field_type') == 'date' or field.get('durability') == 'expires':
            pack.date_sources.append(dict(
                id=f"crm_field:{field.get('entity')}:{field.get('key')}", label=label,
                record=f"crm_{field.get('entity')}", field=field.get('key'),
                durability=field.get('durability') or 'once'))
    stages = pack.guarded('stages_unavailable', read.stages) or []
    if stages:
        pack.add('record', 'stages', 'config', 'Stages: ' + ', '.join(_text(s.get('name'), 40) for s in stages[:20]),
                 dict(record='crm_stages', names=[_text(s.get('name'), 40) for s in stages[:20]],
                      types=[s.get('stage_type') for s in stages[:20]]),
                 dict(type='crm_stages'))

    # 11. Counts of work already recorded; never message content.
    for table in ('crm_contacts', 'crm_companies', 'requirement_instances', 'crm_tasks', 'bookings'):
        count = pack.guarded(f'{table}_count_unavailable', lambda t=table: read.count(t))
        if count:
            pack.add('count', table, 'derived', f'{table}: {count}', dict(table=table, count=count),
                     dict(type='count', reference=table))

    # 12. Values the interview captured for later pages: always to be confirmed.
    for index, item in enumerate(config.get('setup_held') or []):
        if isinstance(item, dict) and item.get('page'):
            pack.add('held', f"{item['page']}:{index}", 'held',
                     f"{item['page']}: {json.dumps(item.get('values'), default=str)[:400]}",
                     dict(page=item['page']), dict(type='setup_held', reference=item['page']))

    return _finish(pack, hours, candidates)


def _finish(pack, hours, candidates):
    pack.signals.sort(key=lambda s: (TRUST.index(s['trust']), s['kind'], s['rank'], s['id']))
    ids = [s['id'] for s in pack.signals]
    duplicate = {i for i in ids if ids.count(i) > 1}
    if duplicate:
        raise AssertionError(f'Duplicate signal ids: {sorted(duplicate)}')
    prefills = {}
    if 'working_days' in hours and 'opening_hours' in hours:
        days = hours['working_days'][0].split(',')
        opening, closing = (v[:5] for v in hours['opening_hours'])
        prefills['diary.hours'] = dict(
            value=dict(working={d: [opening, closing] for d in days}, bank_holidays_closed=None,
                       contact_same=True, contact=None, notes=''),
            words=None, signal_ids=['booking_setting:working_days', 'booking_setting:opening_hours'])
    else:
        words = next((c for c in candidates if c['topic'] == 'hours' and c['state'] == 'answered'
                      and c.get('answer')), None)
        if words:
            prefills['diary.hours'] = dict(value=None, words=_text(words['answer'], 600),
                                           signal_ids=['knowledge_answer:' + words['id'].split('.', 1)[1]])
    revision = _hash([(s['id'], s['trust'], _hash(s['value'])) for s in pack.signals]
                     + [sorted(d['id'] for d in pack.date_sources)])
    return dict(version=1, evidence_revision=revision, signals=pack.signals,
                date_sources=sorted(pack.date_sources, key=lambda d: d['id']),
                prefills=prefills, gaps=sorted(set(pack.gaps)), limits=dict(truncated=False))


def signal(pack, identity):
    return next((s for s in pack['signals'] if s['id'] == identity), None)


def model_view(pack, step):
    """The slice for one step's model turn, lowest trust dropped first to fit."""
    if step not in STEPS:
        raise ValueError('Unknown setup step.')
    signals = [dict(id=s['id'], kind=s['kind'], trust=s['trust'], text=s['text']) for s in pack['signals']]
    view = dict(step=step, evidence_revision=pack['evidence_revision'], signals=signals,
                date_sources=[dict(id=d['id'], label=d['label']) for d in pack['date_sources']],
                gaps=pack['gaps'], truncated=False)
    while len(json.dumps(view, separators=(',', ':'))) > MODEL_CAP and view['signals']:
        view['signals'].pop()
        view['truncated'] = True
    return view
