from copy import deepcopy
from uuid import uuid4
import pytest
from app import knowledge_review as kr


@pytest.fixture
def review_store(monkeypatch):
    class Store:
        rows = []
        sources = []
        brief = ""
        def load(self, tenant):
            data = {"brief":self.brief,"sources":deepcopy(self.sources),"reviews":deepcopy(self.rows)}
            return {**data,"version":kr.fingerprint(data)}
        def stage(self, tenant, entries, *, origin="Owner note", questions=None, mailbox_id=None, return_to="/onboarding/knowledge",source_units=None,capture_manifest=None,authority_context=None):
            row = {"id":str(uuid4()),"tenant_id":tenant,"state":"pending","payload":{
                "entries":[{**n,"id":str(uuid4()),"mailbox_id":mailbox_id,"provenance":n.get("provenance") or [{"source":origin}]} for n in entries],
                "questions":questions or [],"origin":origin,"return_to":return_to,"topic_reviews":{},"source_units":source_units or [],"capture_manifest":capture_manifest or []}}
            self.rows.append(row)
            return row
    from types import SimpleNamespace
    from app import db
    store = Store()
    def rpc(name,args):
        assert name=='checkpoint_knowledge_review'
        assert args['p_expected']==store.load(args['p_tenant'])['version']
        row=next(r for r in store.rows if r['id']==args['p_review'])
        row['payload']['draft_review_choices']=deepcopy(args['p_choices'])
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=store.load(args['p_tenant'])))
    monkeypatch.setattr(db,'db',lambda:SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(kr,"load",store.load)
    monkeypatch.setattr(kr,"stage",store.stage)
    return store
