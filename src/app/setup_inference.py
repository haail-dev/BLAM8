"""Which proposed setup values the guide may keep, and with what reason.

The help model proposes rows whose fields each carry a source. Fields the owner
was never asked about are the usual case, not an error: the native editors give
every row a blank-row default, and a proposal may use that default as the
"usual setting". A value inferred from this business's own facts is kept when
it cites evidence from the setup context pack. Anything else is replaced by the
usual setting or held back with a plain question, so one unasked switch never
sinks the rest of a proposal.

Nothing here saves settings; the owner's Accept remains the only write.
"""
from copy import deepcopy

from app import crm_visibility, setup_context, setup_wording

USUAL = 'Usual setting'
# Signals an inferred value may rest on: the owner's confirmed or submitted
# facts, saved configuration, and their own saved website and Knowledge text.
EVIDENCE_TRUST = setup_context.SUGGEST_TRUST | {'owner_saved_source'}
MAX_EVIDENCE = 3
MAX_REASON = 200


def _staff_default(fields):
    return crm_visibility.default_staff_can_see(fields)


# The blank row each native editor creates. A callable receives the row's other
# fields. Keep in step with the editors (tests compare them).
NATIVE_DEFAULTS = {
    ('booking_details', 'field'): {'required': False, 'kind': 'answer', 'hint': '',
                                   'services': [], 'remember': 'ask'},
    ('dependants', 'group'): {'cardinality': 'many', 'plural': ''},
    ('dependants', 'field'): {'type': 'text', 'options': [], 'required': False,
                              'durability': 'once', 'gate': False, 'hint': ''},
    ('crm_fields', 'contact_field'): {'field_type': 'text', 'options': [], 'required': False,
                                      'probe_behaviour': 'passive', 'extraction_hint': '',
                                      'durability': 'once', 'staff_can_see': _staff_default},
    ('crm_settings', 'stage'): {'stage_type': 'active', 'display_order': 100},
}

# Choices a new row must state explicitly (setup_rows.parse, setup_card_fields,
# setup_card_crm). When the model leaves one out it gets the usual setting, the
# same as an unasked one; the remaining fields default inside their parsers.
FILL_MISSING = {
    ('booking_details', 'field'): ('required',),
    ('dependants', 'group'): ('cardinality',),
    ('dependants', 'field'): ('type', 'required', 'durability', 'gate'),
    ('crm_fields', 'contact_field'): ('field_type', 'required', 'probe_behaviour', 'durability', 'staff_can_see'),
    ('crm_settings', 'stage'): ('stage_type', 'display_order'),
}

# Fields whose value changes what customers are asked, refused or told.
CUSTOMER_AFFECTING = {
    ('booking_details', 'field'): {'required', 'services', 'remember'},
    ('dependants', 'field'): {'required', 'gate', 'durability', 'recall_days'},
    ('dependants', 'ladder'): {'gates', 'notify'},
    ('crm_fields', 'contact_field'): {'required', 'probe_behaviour'},
}


# Rows these rules know how to settle. Other rows (diary settings, invoices,
# scalar CRM settings) keep the stricter rule: an assumed value is refused.
SETTLED_ROWS = set(NATIVE_DEFAULTS) | set(CUSTOMER_AFFECTING)
ASSUMED = 'A choice was assumed rather than supplied. Please answer it explicitly; no suggestion was stored.'


def default(page, row, field, fields):
    """(True, value) when the native editor has a blank-row default, else (False, None)."""
    table = NATIVE_DEFAULTS.get((page, row), {})
    if field not in table:
        return False, None
    value = table[field]
    return True, deepcopy(value(fields) if callable(value) else value)


def customer_affecting(page, row, field):
    return field in CUSTOMER_AFFECTING.get((page, row), set())


def evidence_ok(source, signals):
    """An inferred source cites 1-3 known signals, at least one the owner stands behind."""
    ids = source.get('evidence') or []
    if not isinstance(ids, list) or not ids or len(ids) > MAX_EVIDENCE:
        return False
    if any(not isinstance(i, str) or i not in signals for i in ids):
        return False
    return any(signals[i] in EVIDENCE_TRUST for i in ids)


def signal_trust(pack):
    """Map signal id -> trust for a setup context pack (missing pack: empty)."""
    return {s['id']: s['trust'] for s in (pack or {}).get('signals', [])}


def _usual():
    return {'kind': 'inferred', 'reference': USUAL, 'evidence': []}


def _row_label(fields, line):
    merged = dict((line or {}).get('fields', {}), **fields)
    return merged.get('label') or merged.get('name') or merged.get('key') or ''


def resolve(page, patches, card, signals, *, strict=True):
    """Return (patches, questions) with every unsupported value settled.

    Per field: owner_said and owner_site stay. inferred stays when its evidence
    checks out; a stricter-than-usual customer-facing value is marked
    ``check`` so the card flags it for the owner. Any other value takes the usual
    setting when there is one (a customer-facing switch also gets a question,
    so the owner can make it stricter), or, for a set on a saved line, is left
    out so the saved value carries forward. A new row missing a field that has
    no usual setting (its name, say) is held back with a question.

    Rows these rules do not cover keep the stricter rule: with ``strict`` an
    assumed or inferred value there refuses the proposal (an owner turn);
    without it the row is left out (a draft the owner never typed).
    """
    lines = {l['id']: l for l in (card or {}).get('lines', [])}
    kept, questions = [], []
    for original in patches:
        patch = deepcopy(original)
        if patch['op'] == 'remove':
            kept.append(patch)
            continue
        line = lines.get(patch.get('id')) if patch['op'] == 'set' else None
        row = line['name'] if line else patch.get('name')
        fields, sources = patch.get('fields', {}), patch.get('sources', {})
        if (page, row) not in SETTLED_ROWS:
            if any(s.get('kind') in ('assumed', 'inferred') for s in sources.values()):
                if strict:
                    raise ValueError(ASSUMED)
                continue
            kept.append(patch)
            continue
        label = _row_label(fields, line)
        held_back = False
        for field in list(fields):
            source = sources.get(field) or {}
            kind = source.get('kind')
            has_default, usual = default(page, row, field, fields)
            if kind in ('owner_said', 'owner_site'):
                continue
            if kind == 'inferred' and (evidence_ok(source, signals) or (not source.get('evidence') and has_default and fields[field] == usual)):
                source = dict(source, evidence=list(source.get('evidence') or []))
                if customer_affecting(page, row, field) and has_default and fields[field] != usual:
                    source['check'] = True
                if not source.get('evidence'):
                    source['reference'] = USUAL
                sources[field] = source
                continue
            asked = setup_wording.question(page, row, field, label)
            if patch['op'] == 'set':
                fields.pop(field)
                sources.pop(field, None)
                if asked:
                    questions.append(asked)
            elif has_default:
                stricter = fields[field] != usual
                fields[field], sources[field] = usual, _usual()
                if stricter and customer_affecting(page, row, field) and asked:
                    questions.append(asked)
            else:
                held_back = True
                if asked:
                    questions.append(asked)
        if held_back or (patch['op'] == 'set' and not fields and 'held' not in patch):
            continue
        if patch['op'] == 'append':
            for field in FILL_MISSING.get((page, row), ()):
                if field not in fields:
                    fields[field] = default(page, row, field, fields)[1]
                    sources[field] = _usual()
        kept.append(patch)
    return kept, list(dict.fromkeys(questions))


# Fields that name the row itself; the card's display line already shows them.
IDENTITY = {'label', 'name', 'key', 'kind'}
SAID = {'owner_said': 'You said', 'owner_site': 'From your website',
        'inferred': 'Suggested', 'assumed': 'Assumed'}


def reasons(page, row, fields, sources):
    """Owner-facing reason lines for one card line.

    Returns ``{'lines': [{'label', 'text', 'check'}], 'usual': [labels]}``. A
    field on its usual setting with no evidence is listed once under
    ``usual`` rather than given its own line.
    """
    from app.onboarding_contract import QUESTIONS

    lines, usual = [], []
    for field in fields:
        source = (sources or {}).get(field)
        if not source or (field in IDENTITY and source.get('kind') == 'owner_said'):
            continue
        name = setup_wording.label(page, row, field)
        if source.get('kind') == 'inferred' and not source.get('evidence'):
            usual.append(name)
            continue
        # Older guided proposals sometimes cite the setup question instead of
        # quoting the owner. Describe that source once and keep the stored audit trail.
        question = QUESTIONS.get(source.get('reference')) if source.get('kind') == 'owner_said' else None
        if question:
            reason = {'label': 'Based on your setup answer', 'text': question['text'],
                      'check': bool(source.get('check'))}
            if reason not in lines:
                lines.append(reason)
            continue
        lines.append({'label': name, 'text': SAID.get(source.get('kind'), 'Source') + ': ' + source.get('reference', ''),
                      'check': bool(source.get('check'))})
    return {'lines': lines, 'usual': usual}
