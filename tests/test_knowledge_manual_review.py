"""Compact review presentation and trusted onboarding header identities."""
from pathlib import Path
from types import SimpleNamespace
import shutil,subprocess
import pytest
from fastapi import HTTPException
from app import onboarding,knowledge_review
from tests.test_knowledge_review import context,entry

ROOT=Path(__file__).parents[1]

@pytest.mark.parametrize('mailbox,account,same', [('owner@example.test','owner@example.test',True),('inbox@example.test','owner@example.test',False),('','owner@example.test',False)])
def test_header_uses_only_tenant_default_nonsecret_mailbox(monkeypatch,mailbox,account,same):
    calls=[]
    def boxes(tenant):
        calls.append(tenant)
        return [{'tenant_id':'tenant-one','address':mailbox},{'tenant_id':'tenant-one','address':'sibling@example.test'}]
    monkeypatch.setattr(onboarding.db,'list_mailboxes',boxes)
    request=SimpleNamespace(state=SimpleNamespace(tenant_id='tenant-one',user_email=account))
    assert onboarding._header_identity(request)=={'account':account,'mailbox':mailbox,'same':same}
    assert calls==['tenant-one']

@pytest.mark.parametrize('kind',['foreign','failure','none','no_tenant'])
def test_header_missing_or_foreign_inbox_keeps_account_only(monkeypatch,kind):
    def boxes(tenant):
        if kind=='no_tenant':pytest.fail('Unauthenticated template must not query mailboxes')
        if kind=='failure':raise RuntimeError('Unavailable')
        return [] if kind=='none' else [{'tenant_id':'foreign','address':'private@example.test'}]
    monkeypatch.setattr(onboarding.db,'list_mailboxes',boxes)
    state=SimpleNamespace(user_email='owner@example.test')
    if kind!='no_tenant':state.tenant_id='tenant-one'
    assert onboarding._header_identity(SimpleNamespace(state=state))=={'account':'owner@example.test','mailbox':'','same':False}


def test_later_is_unresolved_and_cannot_pass_existing_admission():
    ctx,row=context([entry()])
    with pytest.raises(HTTPException) as error:
        knowledge_review.plan(ctx,row,{'version':ctx['version'],'action_one':'','review_later_one':'1'})
    assert error.value.status_code==400 and 'every draft' in error.value.detail


def test_manual_review_controller_uses_actual_local_controls_and_batch_identity():
    result=subprocess.run([shutil.which('node'),'tests/replays/knowledge_manual_review.mjs'],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_header_drum_pairs_identity_faces_and_honors_reduced_motion():
    result=subprocess.run([shutil.which('node'),'tests/replays/onboarding_header.mjs'],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_reviewed_keep_does_not_bypass_existing_overlap_gate():
    note=entry();ctx,row=context([note],sources=[{'source':'Existing terms','content':note['content'],'mailbox_id':None}])
    with pytest.raises(HTTPException) as error:
        knowledge_review.plan(ctx,row,{'version':ctx['version'],'action_one':'keep'})
    assert error.value.status_code==409 and 'overlap' in error.value.detail.lower()
