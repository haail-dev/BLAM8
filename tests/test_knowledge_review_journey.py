"""One-pass review saves approved notes, never silently admits unused evidence."""
from copy import deepcopy
import pytest
from fastapi import HTTPException
from app import knowledge_review as kr, knowledge_guidance as guide, knowledge_fidelity as fidelity
from tests.test_knowledge_review import context, entry


def fixture():
    ctx,row=context([entry(content='We offer support by email.')],brief='')
    row['payload']['source_units']=fidelity.inventory([{'url':'https://example.test', 'text':'We offer support by email.\nExtra work costs £70.\nFictional test facts only.'}])
    return ctx,row,{'version':ctx['version'],'action_one':'keep','review_scope':'notes'}


def test_reviewed_notes_reach_save_without_second_source_interview():
    ctx,row,form=fixture();original=deepcopy(row)
    assert guide.present(ctx,row,form)['save_ready']
    notes,payload=kr.plan(ctx,row,form)
    assert len(notes)==1 and '£70' not in notes[0]['content']
    extra=next(u for u in payload['source_decisions'] if '£70' in u['text'])
    assert extra['status']=='deferred_unknown'
    assert 'Fictional test facts only.' in notes[0]['source_context']
    assert row==original


def test_strict_legacy_review_still_requires_source_decisions():
    ctx,row,form=fixture();form.pop('review_scope')
    assert guide.present(ctx,row,form)['focus']['kind']=='source_batch'
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)


@pytest.mark.parametrize('choice',['exclude','covered:missing','note:missing'])
def test_notes_scope_does_not_hide_explicit_invalid_source_choices(choice):
    ctx,row,form=fixture()
    unit=next(u for u in row['payload']['source_units'] if '£70' in u['text'])
    form['source_'+unit['id']]=choice
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)
    assert not guide.present(ctx,row,form)['save_ready']


def test_unknown_cannot_hide_a_fact_still_in_retained_notes():
    ctx,row,form=fixture()
    unit=next(u for u in row['payload']['source_units'] if u['text']=='We offer support by email.')
    form['source_'+unit['id']]='unknown'
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)


def test_notes_scope_never_approves_an_unreviewed_note_or_overlap():
    ctx,row,form=fixture();form['action_one']=''
    assert guide.present(ctx,row,form)['focus']['kind']=='note'
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)
    form['action_one']='keep';ctx['brief']='We offer support by email.'
    assert guide.present(ctx,row,form)['focus']['kind']=='overlap'
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)


def test_notes_scope_preserves_explicit_bulk_source_choice():
    ctx,row,form=fixture();form['source_default']='note:missing'
    with pytest.raises(HTTPException):kr.plan(ctx,row,form)
