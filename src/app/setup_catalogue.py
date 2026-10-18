"""Trade-neutral authoring catalogue for durable setup proposals."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit
from app import settings_recommend, setup_rows, setup_card_scalars, setup_card_invoices, setup_card_crm, setup_card_mode

FOUNDATION = (
    'Propose settings only from explicit owner answers or supplied owner source material. '
    'A business description is context, never evidence for a duration, price, capacity, '
    'required field, notification policy or other setting. Ask the catalogue question '
    'when an answer makes that control relevant. Preserve existing choices unless the '
    'owner changes them. Unanswered choices remain unanswered; never fill a default. '
    'Use sourced setup_changes. Preview validates dependent choices before acceptance. '
    'Proposing, holding and accepting are different states; only a confirmed receipt means saved.'
)

_CONTROLS = json.loads(Path(__file__).with_name('setup_catalogue_controls.json').read_text(encoding='utf-8'))
ROW_QUESTIONS = {
    'service': ('You offer a named service.', 'What are its duration, price, timing, capacity pool and available weekdays?'),
    'resource': ('You describe limited parallel capacity.', 'What is the capacity pool called, how many are available, and is there a rate?'),
    'intake': ('You need a detail from the customer before booking.', 'What detail is needed, when is it required, and should it be remembered?'),
    'group': ('Bookings concern separately tracked subjects.', 'What is the subject kind called, and can each customer have one or many?'),
    'subject': ('You need a detail about a separately tracked subject.', 'Which kind needs this detail, what format is it, and does expiry or verification apply?'),
    'ladder': ('You describe ordered progress.', 'Who progresses, through which levels, with which notifications and service prerequisites?'),
    'note': ('You supply a fact or operating rule.', 'What is the complete note, and which exact label should it use?'),
    'contact_field': ('You need a fact retained on a customer record.', 'What fact, format, collection behaviour, expiry and staff visibility should apply?'),
    'stage': ('You describe a pipeline step.', 'What is its name, type and position?'),
    'package': ('You sell a balance of sessions or hours.', 'What quantity, unit, price, expiry, services and requirement apply?'),
    'service_urgent': ('You explicitly offer a service on the same day.', 'Which service, cut-off and notice apply, or should this rule be off?'),
    'service_out_of_hours': ('You explicitly offer a service outside normal opening hours.', 'Which times, days, prices, price basis, offer policy and terms apply?'),
    'service_after_job': ('You describe work due after completion.', 'Which service leaves which tasks, with how many calendar or working days from completion?'),
}


def controls():
    return deepcopy(_CONTROLS)


def scalar_specs():
    return {
        'diary_settings': deepcopy(setup_card_scalars.SPECS),
        'invoice_settings': deepcopy(setup_card_invoices.specs('invoice_settings')),
        'packages': deepcopy(setup_card_invoices.specs('packages')),
        'crm_settings': deepcopy(setup_card_crm.SPECS),
        'booking_details': deepcopy(setup_card_mode.SPECS),
    }


def entries():
    result = []
    for page, fields in scalar_specs().items():
        for key, spec in fields.items():
            result.append(dict(id=page+'/'+key, page=page, name='setting', key=key,
                label=spec['label'], controls={'value': spec['type']},
                options={k: v for k, v in spec.items() if k not in {'path', 'label'} and v is not None},
                unset_means='No new owner decision. Keep the saved choice; do not infer a default or an active rule.',
                unset_scope='proposal', ask_when='The owner supplies or requests a change to '+spec['label'].lower()+'.',
                question='What should '+spec['label'].lower()+' be?', card_can_set=True,
                where_on_page=settings_recommend.PAGES[page]['href']))
    for page, name in sorted(setup_rows.SUPPORTED):
        if name == 'setting': continue
        kind = ('intake' if page == 'booking_details' else 'subject') if name == 'field' else name
        when, question = ROW_QUESTIONS[kind]
        result.append(dict(id=page+'/'+name, page=page, name=name, label=name.replace('_',' ').capitalize(),
            controls=deepcopy(_CONTROLS[kind]), options={k: v for k, v in _CONTROLS[kind].items() if isinstance(v, list)},
            unset_means='No additional row is proposed. Existing rows are retained; leaving a proposal does not delete saved data.',
            unset_scope='proposal', ask_when=when, question=question, card_can_set=True,
            where_on_page=settings_recommend.PAGES[page]['href']))
    # Keep unsupported catalogue controls visible without promising acceptance.
    covered = {e['id'] for e in result}
    for page, spec in settings_recommend.PAGES.items():
        for key in spec['fields']:
            if page+'/'+key in covered: continue
            result.append(dict(id=page+'/'+key, page=page, name=key, label=key.replace('_',' ').capitalize(),
                controls={}, options={}, unset_means='No proposal is applied.', unset_scope='proposal',
                ask_when='The owner explicitly asks about this control.', question='What would you like to change here?',
                card_can_set=False, where_on_page=spec['href']))
    return sorted(result, key=lambda e: e['id'])


def lookup(identity):
    match = next((e for e in entries() if e['id'] == identity), None)
    if match is None: raise ValueError('Unknown setup catalogue entry.')
    return match


def index():
    lines = []
    for entry in entries():
        shape = entry['options'] if entry['name'] == 'setting' else entry['controls']
        if entry['name'] == 'setting':
            shape = {k: v for k, v in shape.items() if k in {'type', 'choices', 'minimum', 'maximum', 'nullable'} and v is not None}
        lines.append(entry['id']+' | '+('card' if entry['card_can_set'] else 'page only')+' | '+entry['label']
                     +' | '+json.dumps(shape, separators=(',', ':')))
    return '\n'.join(lines)


def context(path):
    target = urlsplit(path or '').path
    return {'index': index(), 'current_page': [e for e in entries()
            if urlsplit(e['where_on_page']).path == target]}
