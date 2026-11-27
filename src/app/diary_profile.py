"""Named diaries: storage, which questions each still needs, and its plain profile line.

Pure functions over the onboarding state (reports/diary-discovery-spec-2026-10-24.md,
Parts 1, 2 and 4). A diary's label is the owner's own words and never drives
behaviour; only its stored answers do. Nothing here writes settings.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from app import diary_support, onboarding_contract as contract
from app.onboarding_state import key_for, stamp

FIELDS = ('diary.example', 'diary.starter', 'diary.date_source', 'diary.occupies', 'diary.capacity',
          'diary.time_shape', 'diary.length', 'diary.repeats', 'diary.repeats_detail',
          'diary.agreement', 'diary.agreement_who')
OPEN = 'unanswered'


def _history(diary, actor, change):
    diary['history'].append(dict(change, at=stamp(), by=actor))


def create(state, label, actor, *, provenance, suggestion=None, seed=None, example=None):
    label = contract.diary_label(label)
    active = contract.active_diaries(state)
    if len(active) >= contract.DIARY_LIMIT:
        raise ValueError(f'You can keep up to {contract.DIARY_LIMIT} diaries here. Remove one to add another.')
    if any(d['label'].casefold() == label.casefold() for d in active):
        raise ValueError(f'You already have a diary called {label}.')
    if provenance not in ('owner_typed', 'owner_selected_suggestion'):
        raise ValueError('Unknown diary label provenance.')
    identity = str(uuid4())
    state['diaries'][identity] = dict(
        id=identity, label=label, label_provenance=provenance, suggestion=deepcopy(suggestion),
        seed=dict(dict(starter=None, date_source=None, service=None), **(seed or {})),
        example=contract.bounded(example, 200) if example else None,
        order=max([d['order'] for d in state['diaries'].values()] or [0]) + 1,
        status='named', support=None, created_at=stamp(), created_by=actor, history=[])
    return identity


def get(state, identity):
    diary = state.get('diaries', {}).get(identity)
    if not diary or diary['status'] == 'removed':
        raise ValueError('Choose one of your diaries.')
    return diary


def rename(state, identity, label, actor):
    diary = get(state, identity)
    label = contract.diary_label(label)
    if any(d['id'] != identity and d['label'].casefold() == label.casefold() for d in contract.active_diaries(state)):
        raise ValueError(f'You already have a diary called {label}.')
    _history(diary, actor, dict(change='rename', previous=diary['label']))
    diary.update(label=label, label_provenance='owner_typed')


def remove(state, identity, actor):
    diary = get(state, identity)
    _history(diary, actor, dict(change='remove', previous=diary['status']))
    diary['status'] = 'removed'


def reorder(state, identities):
    active = [d['id'] for d in contract.active_diaries(state)]
    if sorted(identities) != sorted(active):
        raise ValueError('List each of your diaries once.')
    for order, identity in enumerate(identities, 1):
        state['diaries'][identity]['order'] = order


def answer(state, identity, question):
    return state['answers'].get(key_for(question, 'diary', identity))


def values(state, identity):
    """Answered values only; open, unsure and deferred answers read as None."""
    result = {}
    for question in FIELDS:
        found = answer(state, identity, question)
        result[question] = found['value'] if found and found['state'] == 'answered' else None
    return result


def applicable(state, identity):
    """The questions this diary still applies to, in asking order (spec §2.2).

    An open earlier answer keeps later questions in the plan, so changing an
    answer re-plans what follows; stored answers to dropped questions remain.
    """
    v = values(state, identity)
    starter, occupies = v['diary.starter'], v['diary.occupies']
    plan = ['diary.example', 'diary.starter']
    if starter == 'from_records':
        return plan + ['diary.date_source']
    plan.append('diary.occupies')
    if occupies != ['nothing']:
        plan.append('diary.capacity')
    plan.append('diary.time_shape')
    if v['diary.time_shape'] in (None, 'set_time', 'window', 'days'):
        plan.append('diary.length')
    plan.append('diary.repeats')
    if v['diary.repeats'] == 'depends':
        plan.append('diary.repeats_detail')
    if starter in (None, 'customers', 'we_arrange', 'both') and occupies != ['nothing']:
        plan.append('diary.agreement')
        if v['diary.agreement'] == 'someone_else':
            plan.append('diary.agreement_who')
    return plan


def choices(state, identity, question):
    """Choice limits that follow from earlier answers."""
    spec = contract.question(question)
    if question == 'diary.time_shape' and values(state, identity)['diary.occupies'] == ['nothing']:
        return ['set_time', 'due_by']
    return spec['choices']


def next_question(state, identity):
    for question in applicable(state, identity):
        found = answer(state, identity, question)
        if not found or found['state'] == OPEN:
            return question
    return None


def profiled(state, identity):
    return next_question(state, identity) is None


def support(state, identity, modules):
    return diary_support.classify(values(state, identity), modules)


# ── The plain profile line (spec §4.2) ────────────────────────────────────────
STARTER = {'customers': 'customers ask for these', 'we_arrange': 'you arrange these',
           'both': 'customers ask for these or you arrange them', 'internal': 'just your team, no customer'}
RESOURCE = {'people': 'someone’s time', 'places': 'a place', 'equipment': 'equipment'}
SHAPE = {'set_time': 'at a set time', 'window': 'in an arrival window', 'days': 'over one or more days',
         'due_by': 'done by a date'}
REPEAT = {'no': 'no set repeat', 'weekly': 'repeats weekly', 'fortnightly': 'repeats fortnightly',
          'monthly': 'repeats monthly', 'six_weekly': 'repeats every six weeks', 'quarterly': 'repeats quarterly'}
UNDECIDED = 'not decided yet'
DAY_NAMES = dict(mon='Monday', tue='Tuesday', wed='Wednesday', thu='Thursday', fri='Friday',
                 sat='Saturday', sun='Sunday')


def _decided(state, identity, question):
    found = answer(state, identity, question)
    return bool(found) and found['state'] == 'answered'


def describe_length(value):
    if value['unit'] == 'depends':
        return value['detail']
    return f'about {value["amount"]} {value["unit"] if value["amount"] != 1 else value["unit"][:-1]}'


def describe(state, identity, date_sources=()):
    """One sentence in plain words from answers only; no model call."""
    v = values(state, identity)
    plan = applicable(state, identity)
    parts = []
    starter = v['diary.starter']
    if starter == 'from_records':
        source = next((d['label'] for d in date_sources if d['id'] == v['diary.date_source']), None)
        if v['diary.date_source'] == 'not_recorded':
            parts.append('follows a date you don’t record yet')
        elif source:
            parts.append(f'follows {source} from your records')
        else:
            parts.append(f'follows a date from your records ({UNDECIDED})')
        return '; '.join(parts)
    parts.append(STARTER.get(starter, f'who starts these is {UNDECIDED}'))
    occupies = v['diary.occupies']
    if occupies == ['nothing']:
        parts.append('they don’t take up anyone’s time')
    elif occupies:
        taken = ' and '.join(RESOURCE[k] for k in occupies)
        capacity = v['diary.capacity']
        parts.append(f'they take up {taken}' + (f', up to {capacity} at once' if capacity else ''))
    else:
        parts.append(f'what they take up is {UNDECIDED}')
    shape = v['diary.time_shape']
    timing = SHAPE.get(shape, f'timing {UNDECIDED}')
    length = v['diary.length']
    if length and 'diary.length' in plan:
        timing += ', ' + describe_length(length)
    parts.append(timing)
    repeats = v['diary.repeats']
    if repeats == 'depends':
        detail = v['diary.repeats_detail']
        parts.append(f'repeats depending on {detail}' if detail else 'repeats depending on something')
    else:
        parts.append(REPEAT.get(repeats, f'repeat {UNDECIDED}'))
    if 'diary.agreement' in plan:
        agreement = v['diary.agreement']
        if agreement == 'customer':
            parts.append('the customer confirms the time')
        elif agreement == 'someone_else':
            parts.append(f'{v["diary.agreement_who"] or "someone outside your team"} needs to agree')
        elif agreement is None:
            parts.append(f'who agrees the time is {UNDECIDED}')
    return '; '.join(parts)


def _times(span):
    return f'{span[0]} to {span[1]}'


def describe_hours(hours):
    if not hours:
        return f'Hours: {UNDECIDED}.'

    def week(days):
        groups = []
        for day in contract.DAY_KEYS:
            if day not in days:
                continue
            if groups and groups[-1][1] == days[day] and contract.DAY_KEYS.index(groups[-1][0][-1]) + 1 == contract.DAY_KEYS.index(day):
                groups[-1][0].append(day)
            else:
                groups.append(([day], days[day]))
        text = []
        for run, span in groups:
            names = DAY_NAMES[run[0]] if len(run) == 1 else f'{DAY_NAMES[run[0]]} to {DAY_NAMES[run[-1]]}'
            text.append(f'{names}, {_times(span)}')
        return '; '.join(text)
    line = f'Hours: {week(hours["working"])}.'
    if hours.get('bank_holidays_closed') is True:
        line += ' Closed on bank holidays.'
    line += (' People can reach you at the same times.' if hours['contact_same']
             else f' People can reach you: {week(hours["contact"])}.')
    return line


def profile(state, modules, date_sources=()):
    lines = []
    for diary in contract.active_diaries(state):
        result = support(state, diary['id'], modules)
        lines.append(dict(id=diary['id'], label=diary['label'], line=describe(state, diary['id'], date_sources),
                          profiled=profiled(state, diary['id']), support=result['state'], kind=result['kind'],
                          reasons=diary_support.wording(result['reasons']), reason_codes=result['reasons']))
    found = state['answers'].get(key_for('diary.hours', 'organisation', 'organisation'))
    hours = found['value'] if found and found['state'] == 'answered' else None
    summary = 'No fixed weekly hours.' if found and found['state'] == 'not_applicable' else describe_hours(hours)
    return dict(diaries=lines, hours=summary,
                hours_state=found['state'] if found else OPEN)
