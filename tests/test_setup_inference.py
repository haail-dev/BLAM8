from app import (crm_fields, requirement_groups, setup_card_details, setup_card_fields,
                 setup_inference as inference, setup_wording as wording)


SIGNALS = {'business:business_brief': 'owner_submitted', 'source_text:abc': 'owner_saved_source',
           'count:contacts': 'derived'}


def said(value, words='owner words'):
    return value, {'kind': 'owner_said', 'reference': words}


def assumed(value, why='not stated'):
    return value, {'kind': 'assumed', 'reference': why}


def inferred(value, why, evidence):
    return value, {'kind': 'inferred', 'reference': why, 'evidence': evidence}


def append(row, **assignments):
    return {'op': 'append', 'name': row, 'fields': {k: v[0] for k, v in assignments.items()},
            'sources': {k: v[1] for k, v in assignments.items()}}


def test_native_defaults_match_each_editors_blank_row():
    booking = setup_card_details.named('booking_details', 'field', {'label': 'Detail'})
    for field, value in inference.NATIVE_DEFAULTS['booking_details', 'field'].items():
        assert booking[field] == value, field
    detail = requirement_groups.clean_field({'label': 'Detail'})
    for field, value in inference.NATIVE_DEFAULTS['dependants', 'field'].items():
        assert detail[field] == value, field
    group = requirement_groups.clean_group({'label': 'Item', 'fields': [{'label': 'Detail'}]})
    assert group['cardinality'] == inference.NATIVE_DEFAULTS['dependants', 'group']['cardinality']
    crm = setup_card_fields.named(crm_fields.validate_definition('contact', 'Detail', 'text', None, ''))
    for field in ('field_type', 'options', 'required', 'probe_behaviour', 'extraction_hint', 'durability'):
        assert crm[field] == inference.NATIVE_DEFAULTS['crm_fields', 'contact_field'][field], field
    assert inference.default('crm_fields', 'contact_field', 'staff_can_see', {'label': 'Door code'}) == (True, False)
    assert inference.default('crm_fields', 'contact_field', 'staff_can_see', {'label': 'Approval note'}) == (True, True)


def test_unasked_defaults_become_the_usual_setting_and_keep_the_row():
    # The shape of the rejected Related records proposal: stated label, unstated gate.
    patches = [append('field', kind=said('Property'), label=said('Address'), type=said('text'),
                      required=said(False), durability=said('confirm'), gate=assumed(False))]
    kept, questions = inference.resolve('dependants', patches, None, SIGNALS)
    assert len(kept) == 1 and questions == []
    assert kept[0]['fields']['gate'] is False
    assert kept[0]['sources']['gate'] == {'kind': 'inferred', 'reference': inference.USUAL, 'evidence': []}
    assert kept[0]['sources']['label']['kind'] == 'owner_said'


def test_stricter_unsupported_customer_switch_is_reset_and_asked():
    patches = [append('field', label=said('Repair description'), required=assumed(True, 'probably needed'))]
    kept, questions = inference.resolve('booking_details', patches, None, SIGNALS)
    assert kept[0]['fields']['required'] is False
    assert questions == ['Would you refuse to go ahead without Repair description?']


def test_non_customer_assumption_takes_the_usual_value_without_a_question():
    patches = [append('field', label=said('Repair description'), hint=assumed('Describe the issue'))]
    kept, questions = inference.resolve('booking_details', patches, None, SIGNALS)
    assert kept[0]['fields']['hint'] == '' and questions == []


def test_inferred_value_with_trusted_evidence_is_kept_and_stricter_ones_are_marked():
    patches = [append('field', label=said('Repair description'),
                      required=inferred(True, 'Your site asks for a description first', ['source_text:abc']))]
    kept, questions = inference.resolve('booking_details', patches, None, SIGNALS)
    source = kept[0]['sources']['required']
    assert kept[0]['fields']['required'] is True and questions == []
    assert source['check'] is True and source['evidence'] == ['source_text:abc']


def test_inferred_evidence_must_be_known_and_trusted():
    weak = inferred(True, 'many contacts', ['count:contacts'])
    unknown = inferred(True, 'made up', ['source_text:zzz'])
    for value in (weak, unknown):
        kept, questions = inference.resolve('booking_details', [append('field', label=said('Detail'), required=value)], None, SIGNALS)
        assert kept[0]['fields']['required'] is False and questions


def test_row_missing_a_field_without_a_usual_value_is_held_back_with_a_question():
    patches = [append('group', label=assumed('Thing'), cardinality=said('many')),
               append('field', kind=said('Property'), label=said('Address'))]
    kept, questions = inference.resolve('dependants', patches, None, SIGNALS)
    assert [p['name'] for p in kept] == ['field']
    assert questions == ['What do you call each one?']


def test_set_on_a_saved_line_drops_the_unsupported_field_and_carries_the_rest():
    card = {'lines': [{'id': 'l1', 'name': 'field', 'status': 'proposed',
                       'fields': {'label': 'Address', 'gate': False}, 'sources': {}}]}
    patch = {'op': 'set', 'id': 'l1', 'fields': {'gate': True, 'hint': 'Street'},
             'sources': {'gate': assumed(True)[1], 'hint': said('Street')[1]}}
    kept, questions = inference.resolve('dependants', [patch], card, SIGNALS)
    assert kept[0]['fields'] == {'hint': 'Street'}
    assert questions == ['If Address has run out, should that stop a booking?']


def test_crm_rows_get_the_choices_their_adapter_demands():
    patches = [append('contact_field', label=said('Repair approval note'))]
    kept, _ = inference.resolve('crm_fields', patches, None, SIGNALS)
    fields = kept[0]['fields']
    assert set(setup_card_fields.REQUIRED) <= set(fields)
    assert fields['probe_behaviour'] == 'passive' and fields['required'] is False
    assert kept[0]['sources']['probe_behaviour']['reference'] == inference.USUAL


def test_customer_affecting_fields_have_owner_words_and_questions():
    for (page, row), fields in inference.CUSTOMER_AFFECTING.items():
        for field in fields:
            assert (page, row, field) in wording.WORDS, (page, row, field)
            assert wording.question(page, row, field, 'X'), (page, row, field)
    for words in wording.WORDS.values():
        assert '_' not in words[0]


def test_filled_rows_pass_their_row_parsers():
    from app import setup_rows
    cases = [('booking_details', 'field', {'label': 'Detail'}),
             ('dependants', 'group', {'label': 'Item'}),
             ('dependants', 'field', {'kind': 'Item', 'label': 'Detail'})]
    for page, row, fields in cases:
        patch = {'op': 'append', 'name': row, 'fields': dict(fields),
                 'sources': {k: {'kind': 'owner_said', 'reference': 'x'} for k in fields}}
        kept, _ = inference.resolve(page, [patch], None, SIGNALS)
        assert setup_rows.parse(page, row, kept[0]['fields'])['problems'] == [], (page, row)


def test_reasons_speak_in_owner_words_and_group_usual_settings():
    from app import setup_cards
    fields = {'kind': 'Property', 'label': 'Address', 'type': 'text', 'required': True,
              'durability': 'once', 'gate': False}
    sources = {'kind': said('Property')[1], 'label': said('Address')[1],
               'type': {'kind': 'inferred', 'reference': inference.USUAL, 'evidence': []},
               'required': {'kind': 'inferred', 'reference': 'Your site asks for it', 'evidence': ['source_text:abc'], 'check': True},
               'durability': said('once', 'stays the same')[1],
               'gate': {'kind': 'inferred', 'reference': inference.USUAL, 'evidence': []}}
    result = inference.reasons('dependants', 'field', fields, sources)
    assert result['usual'] == ['Format', 'Stops a booking when it has run out']
    assert result['lines'] == [
        {'label': 'Must be filled in', 'text': 'Suggested: Your site asks for it', 'check': True},
        {'label': 'How long it stays true', 'text': 'You said: stays the same', 'check': False}]
    card = setup_cards.change('dependants', None, 0, 'turn-1', [{'op': 'append', 'name': 'field', 'fields': fields, 'sources': sources}])
    assert card['lines'][0]['reasons'] == result


def test_saved_question_reference_is_explained_once_without_quoting_the_owner():
    fields = {'required': True, 'remember': 'ask', 'hint': 'Give the date'}
    sources = {key: {'kind': 'owner_said', 'reference': 'requirements.policy'} for key in fields}
    sources['hint'] = {'kind': 'owner_said', 'reference': 'Give the date'}
    reasons = inference.reasons('booking_details', 'field', fields, sources)
    assert reasons['lines'] == [
        {'label': 'Based on your setup answer', 'text': 'What must be known or checked before proceeding?', 'check': False},
        {'label': 'Prompt shown to customers', 'text': 'You said: Give the date', 'check': False},
    ]


def test_rows_without_usual_settings_keep_the_strict_rule():
    import pytest
    for page, row in (('invoice_settings', 'setting'), ('diary_settings', 'resource'), ('crm_settings', 'setting')):
        patch = append(row, key=said('x'), value=assumed('y'))
        with pytest.raises(ValueError, match='assumed rather than supplied'):
            inference.resolve(page, [patch], None, SIGNALS)
        stated = append(row, key=said('x'), value=said('y'))
        assert inference.resolve(page, [stated], None, SIGNALS) == ([stated], [])
        assert inference.resolve(page, [patch], None, SIGNALS, strict=False) == ([], [])


def test_service_with_unknown_price_asks_for_that_price_only():
    for price in (assumed(20), inferred(20, 'unverified', ['unknown:id'])):
        patch = append('service', name=said('Visit'), minutes=said(30), timing=said('time'), price=price)
        assert inference.resolve('diary_settings', [patch], None, SIGNALS) == ([], ['What price should I show for Visit?'])
        assert inference.resolve('diary_settings', [patch], None, SIGNALS, strict=False) == ([], [])
