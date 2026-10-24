"""The diary discovery conversation: cached context, suggestions, and owner words to answers.

reports/diary-discovery-spec-2026-10-24.md, Parts 0–4. The model reads the
shared business picture and the owner's message and returns data only: diary
names, typed answers and a reply. The server validates every id, value and
cited fact, saves what holds up in one revision, and writes the acknowledgement
from what was actually saved. Nothing here proposes or applies settings.
"""
from __future__ import annotations

import json

from app import diary_profile, onboarding_contract as contract, setup_context
from app import guided_setup as guide
from app.onboarding_state import _confirmed_answer, key_for, stamp

ANSWERABLE = [q for q, spec in contract.QUESTIONS.items()
              if spec.get('discovery') and q not in ('diary.list', 'diary.profile')]
SUGGESTIONS = 5
VALUE_GUIDE = ('value_json is the JSON text of the value. choice: "we_arrange". multi: ["people"]. count: 3. '
               'length: {"amount": 2, "unit": "hours"}. hours: {"working": {"mon": ["09:00", "16:00"]}, '
               '"contact_same": true}. short_text or reference: "words". Use "null" when state is not answered.')
RULES = (
    'DIARY DISCOVERY. You are learning how this business uses diaries and calendars. Do not propose settings. '
    'Use only facts in DIARY CONTEXT and never infer anything from the business type or industry name. '
    'Ask nothing a confirmed fact already answers; offer it for confirmation instead. '
    'Return every answer the owner’s message contains, for any of their diaries, using only the listed question ids '
    'and values, and name the diary by its id or exact label. Return a diary in new_diaries only when the owner names '
    'something they keep a diary or calendar for. If the owner describes something the platform cannot do, record what '
    'they said and never map it to a nearby option. Hours stay a proposal for the owner to confirm. '
    'Keep the reply short and plain, ask at most one question, and never claim anything was saved.')


# ── Cached context (Part 0) ───────────────────────────────────────────────────
def refresh(context, state):
    """Build the shared read and cache what discovery shows. No model call."""
    pack = setup_context.build({**context, 'state': state})
    saved = state['guide'].setdefault('diary', {})
    prefills = dict(pack['prefills'])
    if saved.get('hours_prefill_dismissed') == pack['evidence_revision']:
        prefills.pop('diary.hours', None)  # The owner said no; it returns only if the facts change.
    saved.update(context_revision=pack['evidence_revision'], date_sources=pack['date_sources'][:40],
                 context_prefills=prefills, gaps=pack['gaps'])
    if saved.get('suggestions_revision') != pack['evidence_revision']:
        saved.update(suggestions=fallback_suggestions(pack, state), suggestions_source='fallback',
                     suggestions_revision=pack['evidence_revision'])
    return pack


def _existing_labels(state):
    return {d['label'].casefold() for d in contract.active_diaries(state)}


def fallback_suggestions(pack, state):
    """Suggestions from saved configuration alone, used when the model is unavailable."""
    result, seen = [], _existing_labels(state)
    for signal in pack['signals']:
        if signal['kind'] == 'service':
            name = signal['value']['name']
            result.append(dict(label=name[:contract.DIARY_LABEL], reason=f'Because it’s a saved activity: {name}',
                               signal_ids=[signal['id']], seed=dict(service=name)))
    for source in pack['date_sources']:
        record, _, rest = source['id'].partition(':')
        signal = ('record:group:' + rest.split(':')[0] if record == 'requirement_group'
                  else 'record:field:' + rest)
        if setup_context.signal(pack, signal):
            result.append(dict(label=(source['label'] + ' dates')[:contract.DIARY_LABEL].strip(),
                               reason=f'Because your records keep: {source["label"]}',
                               signal_ids=[signal], seed=dict(date_source=source['id'])))
    return _numbered([s for s in result if s['label'].casefold() not in seen and not seen.add(s['label'].casefold())])


def _numbered(items):
    return [dict(item, id=f's{i}') for i, item in enumerate(items[:SUGGESTIONS], 1)]


def clean_suggestions(raw, pack, state):
    """Keep only suggestions that cite real facts, at least one of them trusted."""
    result, seen = [], _existing_labels(state)
    sources = {d['id'] for d in pack['date_sources']}
    services = {s['value']['name'] for s in pack['signals'] if s['kind'] == 'service'}
    for item in raw if isinstance(raw, list) else []:
        try:
            label = contract.diary_label(item.get('label'))
            reason = contract.bounded(item.get('reason'), 120)
        except (ValueError, AttributeError):
            continue
        cited = [setup_context.signal(pack, i) for i in item.get('signal_ids') or [] if isinstance(i, str)]
        if not cited or None in cited or not any(s['trust'] in setup_context.SUGGEST_TRUST for s in cited):
            continue
        date_source, service = item.get('date_source'), item.get('service')
        if (date_source and date_source not in sources) or (service and service not in services):
            continue
        if label.casefold() in seen:
            continue
        seen.add(label.casefold())
        seed = {k: v for k, v in dict(date_source=date_source, service=service).items() if v}
        result.append(dict(label=label, reason=reason, signal_ids=[s['id'] for s in cited][:3], seed=seed))
    return _numbered(result)


SUGGEST_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['suggestions'], 'properties': {
    'suggestions': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'required': ['label', 'reason', 'signal_ids', 'date_source', 'service'], 'properties': {
            'label': {'type': 'string'}, 'reason': {'type': 'string'},
            'signal_ids': {'type': 'array', 'items': {'type': 'string'}},
            'date_source': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
            'service': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}}}}}


async def _call(tenant_id, system, messages, schema):
    from app import concierge, llm
    from app.config import settings
    msg = await llm.create(client=concierge.client(), tenant_id=tenant_id, operation='concierge_help',
                           module='concierge', model=settings.anthropic_model, max_tokens=4000,
                           system=[{'type': 'text', 'text': system}], messages=messages,
                           output_config={'format': {'type': 'json_schema', 'schema': schema}})
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', None) == 'text').strip()
    return json.loads(text), msg


async def suggest(tenant_id, pack, state):
    system = (RULES + ' Suggest up to five diaries this business may keep, each in a short plain label, with a one-line '
              'reason naming the fact it rests on and the ids of the signals it cites. Cite a date source or saved '
              'service only by its exact id or name. Existing diaries: '
              + json.dumps([d['label'] for d in contract.active_diaries(state)], ensure_ascii=False)
              + '\nDIARY CONTEXT: ' + json.dumps(setup_context.model_view(pack, 'diary'), ensure_ascii=False))
    data, msg = await _call(tenant_id, system, [{'role': 'user', 'content': 'Suggest diaries.'}], SUGGEST_SCHEMA)
    return clean_suggestions(data.get('suggestions'), pack, state), msg


# ── Interpreting the owner's words (Parts 1.6, 2.5, 3.2) ─────────────────────
INTERPRET_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['reply', 'new_diaries', 'answers'],
    'properties': {
        'reply': {'type': 'string'},
        'new_diaries': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['label', 'example'], 'properties': {
                'label': {'type': 'string'}, 'example': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}}},
        'answers': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['question', 'diary', 'state', 'value_json'], 'properties': {
                'question': {'type': 'string', 'enum': ANSWERABLE},
                'diary': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                'state': {'type': 'string', 'enum': ['answered', 'unknown', 'deferred', 'not_applicable']},
                'value_json': {'type': 'string'}}}}}}


def _catalogue():
    return [dict(id=q, text=contract.QUESTIONS[q]['text'], kind=contract.QUESTIONS[q]['kind'],
                 choices=contract.QUESTIONS[q]['choices']) for q in ANSWERABLE]


async def interpret(tenant_id, stop, text, history, pack, state):
    diaries = [dict(id=d['id'], label=d['label'], answers={q: v for q, v in diary_profile.values(state, d['id']).items() if v is not None})
               for d in contract.active_diaries(state)]
    system = (RULES + '\nCurrent question: ' + json.dumps(dict(id=stop['question_id'], text=stop['question'],
              diary=stop.get('diary')), ensure_ascii=False)
              + '\nDiaries: ' + json.dumps(diaries, ensure_ascii=False)
              + '\nQuestions: ' + json.dumps(_catalogue(), ensure_ascii=False) + '\n' + VALUE_GUIDE
              + '\nDate sources: ' + json.dumps([dict(id=d['id'], label=d['label']) for d in pack['date_sources']]
                                                + [dict(id='not_recorded', label='A date we don’t record yet')], ensure_ascii=False)
              + '\nDIARY CONTEXT: ' + json.dumps(setup_context.model_view(pack, 'diary'), ensure_ascii=False))
    return await _call(tenant_id, system, (history or []) + [{'role': 'user', 'content': text}], INTERPRET_SCHEMA)


PHRASE = {
    'diary.starter': lambda v: diary_profile.STARTER.get(v, 'it follows dates from your records'),
    'diary.occupies': lambda v: 'it doesn’t take up anyone’s time' if v == ['nothing'] else
        'it takes up ' + ' and '.join(diary_profile.RESOURCE[k] for k in v),
    'diary.capacity': lambda v: f'up to {v} at once',
    'diary.time_shape': lambda v: diary_profile.SHAPE[v],
    'diary.length': lambda v: f'about {v["amount"]} {v["unit"] if v["amount"] != 1 else v["unit"][:-1]}',
    'diary.repeats': lambda v: 'it repeats depending on something' if v == 'depends' else diary_profile.REPEAT[v],
    'diary.repeats_detail': lambda v: f'it depends on {v}',
    'diary.agreement': lambda v: {'no': 'no one else needs to agree', 'customer': 'the customer confirms the time',
                                  'someone_else': 'someone outside your team needs to agree'}[v],
    'diary.agreement_who': lambda v: f'{v} needs to agree',
    'diary.example': lambda v: f'for example “{v}”',
    'diary.date_source': lambda v: 'it follows a date you don’t record yet' if v == 'not_recorded' else 'it follows a date from your records',
}


def _resolve(state, name):
    if not isinstance(name, str):
        return None
    for diary in contract.active_diaries(state):
        if name == diary['id'] or name.strip().casefold() == diary['label'].casefold():
            return diary
    return None


def _prefill(state, scope, question, value, *, change=False, words=None, source='conversation'):
    prefills = state['guide'].setdefault('diary', {}).setdefault('prefills', {})
    prefills.setdefault(scope, {})[question] = dict(value=value, words=words, change=change, source=source, at=stamp())


def apply(state, stop, data, owner_text, actor, date_sources):
    """Validate and save what the model found. Returns the server-written acknowledgement."""
    notes, problems, created, proposed = [], [], [], []
    saved = state['guide'].setdefault('diary', {})
    for item in data.get('new_diaries') or []:
        try:
            label = contract.diary_label(item.get('label'))
        except (ValueError, AttributeError):
            continue
        if _resolve(state, label):
            continue
        if label.casefold() not in owner_text.casefold():
            # A name the owner did not use is offered for them to accept, never saved.
            if label.casefold() not in {p['label'].casefold() for p in saved.get('proposed_labels', [])}:
                saved.setdefault('proposed_labels', []).append(dict(label=label, example=item.get('example')))
                proposed.append(label)
            continue
        try:
            diary_profile.create(state, label, actor, provenance='owner_typed',
                                 example=(item.get('example') or None) and contract.bounded(item['example'][:200], 200))
            created.append(label)
        except ValueError as exc:
            problems.append(str(exc))
    hints_only = stop['kind'] == 'diaries'
    acknowledged = {}
    for item in data.get('answers') or []:
        question, status = item.get('question'), item.get('state')
        if question not in ANSWERABLE or status not in ('answered', 'unknown', 'deferred', 'not_applicable'):
            continue
        spec = contract.question(question)
        try:
            value = json.loads(item.get('value_json') or 'null') if status == 'answered' else None
            if status == 'answered':
                value = contract.answer_value(spec, value)
        except (ValueError, TypeError):
            if question == 'diary.hours':
                _prefill(state, 'organisation', 'diary.hours', None, words=contract.bounded(owner_text[:600], 600, empty=True))
                notes.append('You mentioned your hours. I’ll check them with you on the hours question.')
            else:
                problems.append(f'I couldn’t use your answer to “{spec["text"]}”.')
            continue
        if question == 'diary.hours':
            if status == 'answered':
                _prefill(state, 'organisation', 'diary.hours', value)
                notes.append('You mentioned your hours: ' + diary_profile.describe_hours(value)[7:-1]
                             + '. I’ll check them with you on the hours question.')
            continue
        diary = _resolve(state, item.get('diary'))
        if not diary:
            problems.append(f'I wasn’t sure which diary “{item.get("diary") or "that"}” meant.')
            continue
        if status == 'answered' and question in ('diary.time_shape',) and value not in diary_profile.choices(state, diary['id'], question):
            problems.append(f'For {diary["label"]}, that timing doesn’t fit something that takes up no one’s time.')
            continue
        if status == 'answered' and question == 'diary.date_source' and value != 'not_recorded' \
                and value not in {d['id'] for d in date_sources}:
            problems.append(f'I couldn’t match that date to your records for {diary["label"]}.')
            continue
        existing = diary_profile.answer(state, diary['id'], question)
        if hints_only:
            _prefill(state, diary['id'], question, value)
            continue
        if existing and existing['state'] == 'answered' and status == 'answered' and existing['value'] != value:
            _prefill(state, diary['id'], question, value, change=True)
            notes.append(f'Earlier you gave a different answer for {diary["label"]}. Confirm the change on that question.')
            continue
        _confirmed_answer(state, spec, 'diary', diary['id'], status, value, actor,
                          provenance=dict(kind='owner_answer', reference='conversation'))
        (state['guide'].get('diary', {}).get('prefills', {}).get(diary['id']) or {}).pop(question, None)
        acknowledged.setdefault(diary['label'], []).append(
            PHRASE[question](value) if status == 'answered' else f'“{spec["text"]}” is {"not decided yet" if status != "not_applicable" else "not needed"}')
    lines = []
    if created:
        lines.append(('Noted a new diary: ' if len(created) == 1 else 'Noted new diaries: ') + ', '.join(created) + '.')
    if proposed:
        lines.append('I’ve suggested a name: ' + ', '.join(proposed) + '. Add it if that’s right.')
    for label, phrases in acknowledged.items():
        lines.append(f'Noted for {label}: ' + '; '.join(phrases) + '.')
    lines += notes + problems
    return ' '.join(lines), bool(created or acknowledged)


def move_on(state):
    """After a save, stay on an open current question, otherwise continue discovery."""
    current = state['guide']['current']
    if current.startswith('diary.ask.') and current != guide.HOURS_STOP:
        _, _, rest = current.partition('diary.ask.')
        identity, _, field = rest.rpartition('.')
        active = identity in {d['id'] for d in contract.active_diaries(state)}
        found = diary_profile.answer(state, identity, 'diary.' + field) if active else None
        if active and 'diary.' + field in diary_profile.applicable(state, identity) and (
                not found or found['state'] == 'unanswered'):
            return
    elif current == guide.HOURS_STOP:
        found = state['answers'].get(key_for('diary.hours', 'organisation', 'organisation'))
        if not found:
            return
    elif current == guide.DIARIES_STOP and not state['answers'].get(key_for('diary.list', 'organisation', 'organisation')):
        return
    state['guide']['current'] = guide.discovery_cursor(state)
    state['guide']['question_revision'] += 1
