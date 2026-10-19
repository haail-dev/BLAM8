import pytest
from app import setup_catalogue as catalogue, setup_rows, setup_card_model, concierge
from tests import agnostic_guard


def test_all_durable_fields_have_one_catalogue_entry_and_matching_controls():
    entries = catalogue.entries()
    assert len({e['id'] for e in entries}) == len(entries)
    for page, name in setup_rows.SUPPORTED:
        if name == 'setting':
            if page == 'booking_details':  # compatibility adapter for existing cards
                continue
            for key in catalogue.scalar_specs()[page]:
                assert catalogue.lookup(page+'/'+key)['card_can_set'] is True
        else:
            entry = catalogue.lookup(page+'/'+name)
            assert set(entry['controls']) == setup_rows.KEYS[page, name]
    for entry in entries:
        assert all(key in entry for key in ('controls', 'options', 'unset_means', 'ask_when', 'question', 'card_can_set', 'where_on_page'))
        assert entry['unset_scope'] == 'proposal'
    with pytest.raises(ValueError): catalogue.lookup('unknown/unknown')
    assert catalogue.lookup('diary_settings/stay_mode')['card_can_set'] is True
    with pytest.raises(ValueError): catalogue.lookup('booking_details/stay_mode')


def test_catalogue_values_are_independent_copies_and_neutral():
    original = catalogue.entries()
    changed = catalogue.entries()
    changed[0]['controls']['invented'] = 'text'
    controls = catalogue.controls()
    controls['service']['minutes'] = 'text'
    assert catalogue.entries() == original
    assert catalogue.controls()['service']['minutes'] == 'number'
    assert not agnostic_guard.scan_render('neutral-setup-catalogue', original)
    assert not agnostic_guard.scan_render('neutral-setup-index', catalogue.index())


@pytest.mark.parametrize('path', ['/app/diary/settings', '/app/diary/intake', '/app/diary/dependants', '/app/invoices/settings#packages', '/app/crm/fields'])
def test_current_page_has_full_entries_and_model_context_stays_bounded(path):
    context = catalogue.context(path)
    assert context['current_page']
    assert len(setup_card_model.context({}, path)) < 65_000


def test_durable_help_has_no_recipe_or_legacy_guessing_instructions():
    for business in ['plumbing', 'dog daycare']:
        tenant = {'industry': business, 'setup_card_context': 'SAVED CARDS'}
        stable, turn = concierge._help_sections(tenant=tenant, settings_guide='FORBIDDEN RECIPE')
        text = '\n'.join(stable + turn)
        assert 'FORBIDDEN RECIPE' not in text and '<settings_guide>' not in text
        assert 'settings recipe below' not in text and 'READY-MADE' not in text
        assert 'list every number you ASSUMED' not in text
        assert 're-issue that card' not in text
        assert 'Set no_dependants=true only when the owner explicitly says' in text
        assert 'submit.kind' in text and 'Plans & billing' in text
        assert catalogue.index() in text


def test_catalogue_and_proposal_context_do_not_take_business_type_as_an_input():
    # Structural invariance; live model proposal equivalence is a separate replay.
    contexts = []
    for business in ['plumbing', 'dog daycare']:
        stable, _ = concierge._help_sections(tenant={'industry': business, 'setup_card_context': 'CARDS'})
        contexts.append(stable)
    assert contexts[0] == contexts[1]
