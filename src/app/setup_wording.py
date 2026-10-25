"""Owner-facing names and questions for setup card fields.

The setup card stores raw field keys such as ``gate`` or ``probe_behaviour``.
Owners never see those keys: the card shows ``label`` and, when a choice still
needs the owner, the server asks ``question`` with the row's own label filled
in. Wording is business-neutral; business nouns come only from the row label
the owner or their saved material supplied.
"""

# (page, row, field) -> (label, question). A question of None means the field is
# never asked on its own; it is shown with the usual setting.
WORDS = {
    ('booking_details', 'field', 'label'): ('Detail', 'What detail do you need?'),
    ('booking_details', 'field', 'required'): ('Needed before going ahead',
        'Would you refuse to go ahead without {label}?'),
    ('booking_details', 'field', 'kind'): ('Answer or document', None),
    ('booking_details', 'field', 'hint'): ('Prompt shown to customers', None),
    ('booking_details', 'field', 'services'): ('Which services need it',
        'Is {label} needed for everything you do, or only some of it?'),
    ('booking_details', 'field', 'remember'): ('Remembered for next time',
        'Should {label} be remembered for next time, or asked each time?'),
    ('dependants', 'group', 'label'): ('Name', 'What do you call each one?'),
    ('dependants', 'group', 'plural'): ('Name for several', None),
    ('dependants', 'group', 'cardinality'): ('One or several per customer',
        'Can a customer have more than one {label}?'),
    ('dependants', 'field', 'kind'): ('Belongs to', 'Which record does {label} belong to?'),
    ('dependants', 'field', 'label'): ('Detail', 'What detail do you need?'),
    ('dependants', 'field', 'type'): ('Format', None),
    ('dependants', 'field', 'options'): ('Choices', 'What are the choices for {label}?'),
    ('dependants', 'field', 'required'): ('Must be filled in',
        'Must {label} always be filled in?'),
    ('dependants', 'field', 'durability'): ('How long it stays true',
        'Does {label} stay the same, need checking when it changes, or run out on a date?'),
    ('dependants', 'field', 'recall_days'): ('Reminder before it runs out',
        'How many days before {label} runs out should you be reminded?'),
    ('dependants', 'field', 'gate'): ('Stops a booking when it has run out',
        'If {label} has run out, should that stop a booking?'),
    ('dependants', 'field', 'hint'): ('What to look out for', None),
    ('dependants', 'ladder', 'who'): ('Who moves up', 'Who moves up through {label}?'),
    ('dependants', 'ladder', 'label'): ('Name', 'What do you call these levels?'),
    ('dependants', 'ladder', 'levels'): ('Levels in order', 'What are the levels of {label}, in order?'),
    ('dependants', 'ladder', 'notify'): ('Telling customers about a move',
        'When someone moves up {label}, should you approve the message first, send it, or not tell them?'),
    ('dependants', 'ladder', 'gates'): ('Levels needed for a service',
        'Does any service need a particular level of {label} first?'),
    ('dependants', 'ladder', 'staff_can_see'): ('Team can see it',
        'Should your team see each customer\'s {label}?'),
    ('crm_fields', 'contact_field', 'label'): ('Detail', 'What detail do you need?'),
    ('crm_fields', 'contact_field', 'field_type'): ('Format', None),
    ('crm_fields', 'contact_field', 'options'): ('Choices', 'What are the choices for {label}?'),
    ('crm_fields', 'contact_field', 'required'): ('Must be filled in',
        'Must every customer record have {label}?'),
    ('crm_fields', 'contact_field', 'extraction_hint'): ('What to look out for', None),
    ('crm_fields', 'contact_field', 'probe_behaviour'): ('Ask customers for it',
        'When {label} is missing, should replies ask customers for it?'),
    ('crm_fields', 'contact_field', 'durability'): ('How long it stays true',
        'Does {label} stay the same, or run out on a date?'),
    ('crm_fields', 'contact_field', 'recall_days'): ('Reminder before it runs out',
        'How many days before {label} runs out should you be reminded?'),
    ('crm_fields', 'contact_field', 'valid_months'): ('Months it stays valid',
        'For how many months does {label} stay valid?'),
    ('crm_fields', 'contact_field', 'staff_can_see'): ('Team can see it', None),
    ('crm_settings', 'stage', 'name'): ('Step', 'What do you call this step?'),
    ('crm_settings', 'stage', 'stage_type'): ('Kind of step', None),
    ('crm_settings', 'stage', 'display_order'): ('Position', None),
}


def label(page, row, field):
    """Owner-facing name for a card field; falls back to readable key text."""
    words = WORDS.get((page, row, field))
    return words[0] if words else field.replace('_', ' ').capitalize()


def question(page, row, field, row_label):
    """Plain question for an unresolved field, or None when it is not asked."""
    words = WORDS.get((page, row, field))
    if not words or not words[1]:
        return None
    name = (row_label or '').strip() or 'this'
    return words[1].format(label=name)
