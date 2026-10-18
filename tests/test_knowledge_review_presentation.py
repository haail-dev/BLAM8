"""Presentation grouping must not change admission or duplicate controls."""
from copy import deepcopy
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader
from app import knowledge_review as kr, knowledge_fidelity as fidelity
from tests.test_knowledge_review import context, entry


def fixture():
    notes = [entry('one', 'Service hours', 'We open at nine.'),
             entry('two', 'Service limits', 'Only confirmed requests proceed.')]
    notes[0]['provenance'] = [{'source': 'https://example.test/hours'}]
    notes[1]['provenance'] = [{'source': 'https://example.test/limits'}]
    ctx, row = context(notes)
    row['payload']['source_units'] = fidelity.inventory([
        {'url': 'https://example.test/hours', 'text': 'Fictional test facts.\nWe open at nine.\nHours vary by agreement.'},
        {'url': 'https://example.test/limits', 'text': 'Fictional test facts.\nOnly confirmed requests proceed.\nNo dispatch is promised.'},
        {'url': 'https://example.test/other', 'text': 'Contact us.'}])
    return ctx, row


def test_note_content_decisions_and_optional_evidence_without_audit_inventory():
    ctx, row = fixture(); before = deepcopy((ctx, row))
    view = kr.review_view(ctx, row)
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1]/'src/app/templates'), autoescape=True)
    html = env.get_template('_knowledge_review.html').render(review=view, csrf_token=lambda r:'token', request={})
    assert (ctx, row) == before
    cards = re.findall(r'<section[^>]*data-draft="[^"]+".*?</section>', html, re.S)
    assert len(cards) == 2
    for card in cards:
        assert '<textarea' in card and '<select name="action_' in card
        assert 'Source and evidence</summary>' in card
        assert 'Fictional test facts.' in card
        assert 'data-source-unit' not in card
    for unit in view['source_view']['units']:
        if unit['kind'] == 'qualification':
            continue
        for name in ['source_'+unit['id'], 'source_reason_'+unit['id']]:
            assert html.count('name="'+name+'"') == 1
            assert '<input type="hidden" name="'+name+'" value="">' in html
    assert 'Source-to-note review' not in html
    assert 'unresolved' not in html and 'literal in note' not in html
    assert 'source text' not in html and 'data-source-unit' not in html


def test_source_choices_and_exclusion_reasons_survive_hidden_form_contract():
    ctx, row = fixture()
    unit = row['payload']['source_units'][2]
    form={'source_'+unit['id']:'exclude','source_reason_'+unit['id']:'Not relevant <owner decision>', 'source_default':'note:two',
          'guided_unknown_question':'1', 'guided_expand_one':'1'}
    view = kr.review_view(ctx, row, form)
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1]/'src/app/templates'), autoescape=True)
    html = env.get_template('_knowledge_review.html').render(review=view, csrf_token=lambda r:'token', request={})
    assert 'name="source_'+unit['id']+'" value="exclude"' in html
    assert 'value="Not relevant &lt;owner decision&gt;"' in html
    assert 'name="source_default" value="note:two"' in html
    assert 'name="guided_unknown_question" value="1"' in html
    assert 'name="guided_expand_one" value="1"' in html
