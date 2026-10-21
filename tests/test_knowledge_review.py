from copy import deepcopy
from pathlib import Path
import pytest
from fastapi import HTTPException
from jinja2 import Environment,FileSystemLoader
from app import knowledge_review as kr


def entry(id="one",label="Prices",content="We charge £50 per service. Our price includes the first visit."):
    return dict(id=id,label=label,content=content,mailbox_id=None,provenance=[{"source":"https://example.test/prices","excerpt":"Our price includes the first visit."}],topics=["prices"])


def context(entries=None,sources=None,brief=""):
    row={"id":"review","state":"pending","payload":{"entries":entries or [],"origin":"Website","questions":[{"topic":"prices","question":"Does the price vary by quantity?"}],"return_to":"/onboarding/knowledge"}}
    return dict(version="version",brief=brief,sources=sources or [],reviews=[row]),row


def form(ctx,row):
    return {"version":ctx["version"],**{"action_"+n["id"]:"keep" for n in row["payload"]["entries"]}}


def test_saved_and_pending_evidence_never_share_a_coverage_count():
    ctx,row=context([entry()],sources=[{"source":"Hours","content":"We open Monday at 9am.","mailbox_id":None}])
    view=kr.topic_view(ctx)
    prices=next(t for t in view["topics"] if t["key"]=="prices")
    assert view["saved_count"]==1 and view["pending_count"]==1
    assert prices["state"]=="unknown" and not prices["saved"] and prices["pending"]
    assert all(q["status"]=="unknown" and not q["answer"] for q in prices["questions"])
    assert any(q["question"]=="Does the price vary by quantity?" for q in prices["questions"])
    notes,payload=kr.plan(ctx,row,form(ctx,row))
    assert len(notes)==1 and payload["topic_reviews"]["prices"]["state"]=="unknown"
    assert "no variation" not in notes[0]["content"].lower()


@pytest.mark.parametrize("kind",["brief","saved","pending"])
def test_duplicate_candidates_show_evidence_and_require_owner_choice(kind):
    original=entry()
    ctx,row=context([original],sources=[{"source":"Existing note","content":original["content"],"mailbox_id":None}] if kind=="saved" else [],
                    brief=original["content"] if kind=="brief" else "")
    if kind=="pending": row["payload"]["entries"].append(entry("two","Other label"))
    matches=kr.candidates(original,ctx,row["id"])
    assert any(m["kind"]==kind and m["excerpt"]==original["content"] and m["provenance"] for m in matches)
    before=deepcopy(ctx)
    with pytest.raises(HTTPException):kr.plan(ctx,row,form(ctx,row))
    assert ctx==before
    f=form(ctx,row)
    for n in row["payload"]["entries"]:
        for m in kr.candidates(n,ctx,row["id"]):f["match_"+m["key"]]="keep_both"
    notes,payload=kr.plan(ctx,row,f)
    assert len(notes)==len(row["payload"]["entries"])
    assert len({n["label"] for n in notes})==len(notes)
    assert all("Source evidence:" in n["content"] for n in notes)


def test_discarding_duplicate_requires_no_keep_both_acknowledgement_or_write():
    ctx,row=context([entry()],brief=entry()["content"])
    notes,payload=kr.plan(ctx,row,{"version":"version","action_one":"discard"})
    assert notes==[] and payload["entries"][0]["decision"]=="discard"
    assert ctx["brief"]==entry()["content"]


def test_short_business_brief_repeated_inside_a_longer_note_is_a_candidate():
    n=entry(label='What we do',content='We are an independent support provider. We also manage scheduled requests.')
    ctx,row=context([n],brief='Independent support provider')
    assert any(m['kind']=='brief' for m in kr.candidates(n,ctx,row['id']))


def test_explicit_saved_merge_retains_original_text_and_both_provenances():
    original=entry()["content"]
    ctx,row=context([entry()],sources=[{"source":"Existing note","content":original,"mailbox_id":None}])
    target=kr.candidates(entry(),ctx,row["id"])[0]
    f={"version":"version","action_one":"merge_saved:"+target["id"]}
    notes,payload=kr.plan(ctx,row,f)
    assert len(notes)==1 and notes[0]["label"]=="Existing note"
    assert notes[0]["content"].startswith(original)
    assert "Existing note" in notes[0]["content"] and "https://example.test/prices" in notes[0]["content"]
    assert payload["entries"][0]["decision"].startswith("merge_saved:")


def test_pending_merge_has_one_admission_preserving_both_sources():
    second=entry("two","Other label",entry()["content"]+" Extra stated detail.")
    second["provenance"]=[{"source":"Owner statement"}]
    ctx,row=context([entry(),second])
    f={"version":"version","action_one":"keep","action_two":"merge_pending:one"}
    notes,payload=kr.plan(ctx,row,f)
    assert len(notes)==1 and "Extra stated detail" in notes[0]["content"]
    assert "Owner statement" in notes[0]["content"] and "https://example.test/prices" in notes[0]["content"]
    assert payload["entries"][1]["decision"]=="merge_pending:one"


def test_edits_and_concurrent_changes_invalidate_previous_overlap_acknowledgements():
    ctx,row=context([entry()],brief=entry()["content"])
    f=form(ctx,row)
    match=kr.candidates(entry(),ctx,row["id"])[0]
    f["match_"+match["key"]]="keep_both"
    f["content_one"]=entry()["content"]+" But the rate is £75."
    with pytest.raises(HTTPException):kr.plan(ctx,row,f)
    f["version"]="old-version"
    with pytest.raises(HTTPException):kr.plan(ctx,row,f)


def test_covered_requires_real_answers_and_not_applicable_is_explicit():
    ctx,row=context([entry()])
    f=form(ctx,row);f["topic_prices"]="covered"
    with pytest.raises(HTTPException):kr.plan(ctx,row,f)
    price=next(t for t in kr.topic_view(ctx)["topics"] if t["key"]=="prices")
    for q in price["questions"]:
        f["question_"+q["id"]]="answered";f["answer_"+q["id"]]="The owner confirms this detail."
    notes,payload=kr.plan(ctx,row,f)
    assert payload["topic_reviews"]["prices"]["state"]=="covered"
    assert "Owner answer:" in notes[-1]["content"]
    f=form(ctx,row);f["topic_prices"]="not_applicable"
    notes,payload=kr.plan(ctx,row,f)
    assert all(q["status"]=="not_applicable" for q in payload["topic_reviews"]["prices"]["questions"])
    assert "explicitly marked" in notes[-1]["content"]


def test_rendered_candidates_are_unselected_and_source_markup_is_escaped():
    n=entry(content='<script>alert(1)</script> We charge £50 per service. Our price includes the first visit.')
    ctx,row=context([n],brief=n["content"])
    env=Environment(loader=FileSystemLoader(Path(__file__).parents[1]/"src/app/templates"),autoescape=True)
    html=env.get_template('_knowledge_review.html').render(review=kr.review_view(ctx,row),csrf_token=lambda r:'token',request={})
    assert "Possible overlap: Business brief" in html and 'data-duplicate-candidate' in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert ' checked>' not in html and '<option value="keep" selected' not in html
    assert 'No variation' not in html


def test_failed_context_read_does_not_look_like_an_empty_knowledge_base(monkeypatch):
    monkeypatch.setattr(kr.db,'db',lambda:None)
    with pytest.raises(HTTPException) as exc:kr.load('tenant')
    assert exc.value.status_code==503


@pytest.mark.asyncio
async def test_admission_preserves_explicit_topic_metadata_and_binds_it_to_saved_content(monkeypatch):
    from types import SimpleNamespace
    from app import setup_card_knowledge,usage
    n=entry(label="General",content="We verify a signed agreement.")
    n["topics"]=["process"]
    ctx,row=context([n]);calls=[]
    async def prepare(patch):
        return {"notes":[{**v,"chunks":[{"content":v["content"],"embedding":"[0.1]"}]} for v in patch["notes"]]},1
    monkeypatch.setattr(setup_card_knowledge,'prepare',prepare)
    monkeypatch.setattr(usage,'record_voyage',lambda *a,**kw:None)
    class Client:
        def rpc(self,name,payload):calls.append((name,payload));return SimpleNamespace(execute=lambda:SimpleNamespace(data=True))
    monkeypatch.setattr(kr.db,'db',lambda:Client())
    assert await kr.admit('tenant',row,ctx,form(ctx,row),reviewer='reviewer-user')==1
    _,rpc=calls[0]
    assert rpc['p_expected']=='version' and rpc['p_tenant']=='tenant'
    assert rpc['p_payload']['reviewed_by']=='reviewer-user' and rpc['p_payload']['reviewed_at']
    assert rpc['p_payload']['original_entries']==row['payload']['entries']
    row['state']='saved';row['payload']=rpc['p_payload']
    ctx['sources']=[{'source':v['label'],'mailbox_id':v.get('mailbox_id'),'content':'\n\n'.join(c['content'] for c in v['chunks'])} for v in rpc['p_notes']]
    view=kr.topic_view(ctx)
    assert next(t for t in view['topics'] if t['key']=='process')['saved']
    ctx['sources'][0]['content']='Completely changed facts.'
    assert not next(t for t in kr.topic_view(ctx)['topics'] if t['key']=='process')['saved']


@pytest.mark.asyncio
async def test_embedding_failure_never_calls_admission_or_changes_drafts(monkeypatch):
    from app import setup_card_knowledge
    ctx,row=context([entry()]);before=deepcopy(ctx)
    async def fail(patch):raise RuntimeError('Embedding unavailable')
    monkeypatch.setattr(setup_card_knowledge,'prepare',fail)
    monkeypatch.setattr(kr.db,'db',lambda:pytest.fail('No write may happen before embeddings succeed'))
    with pytest.raises(RuntimeError):await kr.admit('tenant',row,ctx,form(ctx,row))
    assert ctx==before


def test_new_unanswered_detail_makes_previously_confirmed_topic_partial():
    ctx,row=context([],sources=[{'source':'Prices','content':'We charge £50.','mailbox_id':None}])
    price=next(t for t in kr.topic_view(ctx)['topics'] if t['key']=='prices')
    evidence=kr.fingerprint([(s['source'],s.get('mailbox_id'),s['content']) for s in ctx['sources']])
    row['state']='saved';row['payload']['topic_reviews']={'prices':{'state':'covered','evidence':evidence,
        'questions':[{**q,'status':'answered','answer':'An explicit owner answer.'} for q in price['questions']]}}
    assert next(t for t in kr.topic_view(ctx)['topics'] if t['key']=='prices')['state']=='covered'
    ctx['reviews'].append({'id':'new-review','state':'pending','payload':{'entries':[],
        'questions':[{'topic':'prices','question':'Does a new service change the quoted rate?'}]}})
    topic=next(t for t in kr.topic_view(ctx)['topics'] if t['key']=='prices')
    assert topic['state']=='partial'
    assert topic['questions'][-1]['status']=='unknown' and topic['questions'][-1]['answer']==''


PARAGRAPHS=["We handle resident requests and keep owners informed.","Every request is logged on the day it arrives.",
            "Routine repairs are arranged with approved contractors.","Owners receive one statement each month."]


def test_native_crlf_and_guide_lf_submissions_share_one_overlap_key():
    # The guide posts textarea.value (LF); a native Save or Check changes and overlaps
    # submission carries the same text as CRLF. Both must hash to one acknowledgement.
    ctx,row=context([entry(content="\n\n".join(PARAGRAPHS))],brief=PARAGRAPHS[0])
    guide={**form(ctx,row),"content_one":"\n\n".join(PARAGRAPHS)}
    native={**form(ctx,row),"content_one":"\r\n\r\n".join(PARAGRAPHS)}
    legacy={**form(ctx,row),"content_one":"\r\r".join(PARAGRAPHS)}
    views=[kr.review_view(ctx,row,f) for f in (guide,native,legacy)]
    keys=[[m["key"] for m in v["entries"][0]["matches"]] for v in views]
    assert len(keys[0])==1 and keys[0]==keys[1]==keys[2]
    assert views[0]["signatures"]==views[1]["signatures"]==views[2]["signatures"]
    assert views[0]["form"]["content_one"]==views[1]["form"]["content_one"]
    # Resubmitting unchanged text with other line endings is not an owner edit.
    assert all("content_segments" not in n for n in kr.edited_entries(row,native))
    assert "content_segments" in kr.edited_entries(row,{**native,"content_one":native["content_one"]+"\r\nAn actual edit."})[0]
    with pytest.raises(HTTPException) as unresolved:kr.plan(ctx,row,native)
    assert unresolved.value.status_code==409
    native["match_"+keys[0][0]]="keep_both"
    notes,payload=kr.plan(ctx,row,native)
    assert len(notes)==1 and "\r" not in notes[0]["content"] and "\r" not in payload["entries"][0]["content"]
    assert payload["entries"][0]["matches"][0]["key"]==keys[0][0]
