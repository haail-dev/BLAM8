"""Native transcript transport segments through real DB helpers and scoped history route."""
import asyncio
import copy
from types import SimpleNamespace
import pytest
from app import db, gptlive as live, concierge_routes as routes, concierge
from tests.test_gptlive import unit, session, fragment
from tests.test_voice_discard import client
REAL_SAVE=db.save_native_transcript
REAL_LIST=db.list_concierge_messages
REAL_ADD=db.add_concierge_message


class MemoryDB:
    def __init__(self):self.rows={};self.operations=[]
    def table(self,name):
        assert name=="concierge_messages"
        return Query(self)


class Query:
    def __init__(self,store):self.store=store;self.filters=[];self.count=40;self.payload=None;self.inverse=False
    def select(self,*a):return self
    def eq(self,key,value):self.filters.append(lambda row:row.get(key)==value);return self
    @property
    def not_(self):self.inverse=True;return self
    def like(self,key,value):
        inverse=self.inverse
        self.filters.append(lambda row: row.get(key,"").startswith(value[:-1]) != inverse);return self
    def order(self,key,desc=False):self.key=key;self.desc=desc;return self
    def limit(self,count):self.count=count;return self
    def insert(self,row):
        self.payload=[dict(row,id="message-"+str(len(self.store.rows)),created_at="2026-10-13T10:00:00Z")];return self
    def upsert(self,rows,on_conflict):
        assert on_conflict=="id"
        self.payload=copy.deepcopy(rows);return self
    def execute(self):
        if self.payload is not None:
            self.store.operations.append(self.payload)
            self.store.rows.update({row["id"]:row for row in self.payload})
            return SimpleNamespace(data=self.payload)
        selected=[row for row in self.store.rows.values() if all(test(row) for test in self.filters)]
        selected=sorted(selected,key=lambda row:row[self.key],reverse=self.desc)[:self.count]
        return SimpleNamespace(data=copy.deepcopy(selected))


@pytest.mark.asyncio
async def test_real_archive_helpers_history_scope_reopen_and_context_limit(unit,monkeypatch):
    from app.templating import templates
    store=MemoryDB();monkeypatch.setattr(db,"db",lambda:store)
    # Restore real helper (unit isolates general native tests with a no-op).
    async_save=REAL_SAVE
    monkeypatch.setattr(db,"db",lambda:store)
    monkeypatch.setattr(db,"save_native_transcript",async_save)
    monkeypatch.setattr(db,"visible_modules",lambda *a,**k:{"crm"})
    monkeypatch.setattr(routes._guards if hasattr(routes,"_guards") else __import__('app.guards',fromlist=['x']),"sees_business_wide",lambda ctx:False)
    s=session()
    for index in range(45):
        await live.handle_event(s,fragment("Observed words "+str(index),index*10000,index*10000+10,"event"+str(index)),unit)
    await live.flush_transcript(s)
    assert len(store.rows)==45 and all(row['user_id']=='u1' and row['tenant_id']=='t1' for row in store.rows.values())
    first=next(iter(store.rows.values()))
    store.rows['backend-user']={**first,'id':'backend-user','content':'Check diary','role':'user','created_at':'2026-01-01T00:00:00+00:00','trail':[]}
    store.rows['backend-answer']={**first,'id':'backend-answer','content':'Verified backend read result','role':'assistant','created_at':'2026-01-01T00:00:01+00:00','trail':[]}
    store.rows['foreign']={**first,'id':'foreign','user_id':'other-user','content':'PRIVATE OTHER USER','created_at':'2027-01-01T00:00:00+00:00'}
    store.rows['foreign-tenant']={**first,'id':'foreign-tenant','tenant_id':'other-tenant','content':'PRIVATE TENANT','created_at':'2027-01-01T00:00:00+00:00'}
    # unit patched list for general tests; exercise original query implementation.
    real_list=REAL_LIST
    monkeypatch.setattr(db,'db',lambda:store);monkeypatch.setattr(db,'visible_modules',lambda *a,**k:{'crm'})
    monkeypatch.setattr(db,'list_concierge_messages',real_list)
    shown=routes.business_history(unit)
    assert len(shown.messages)==3 and not any('PRIVATE' in row['content'] for row in shown.messages)
    context=routes.business_history(unit,context_only=True)
    assert [row['content'] for row in context.messages]==['Check diary','Verified backend read result']
    assert concierge.sanitize_history(context.messages,16,3000)==[{'role':'user','content':'Check diary'},{'role':'assistant','content':'Verified backend read result'}]
    html=templates.env.get_template('app/_concierge_panel.html').render(chat_history=shown.messages,full_qa=True)
    assert html.count('data-cz-observation=')==1 and 'Observed speech' not in html   # the section's tag says the mode; no per-message repeat
    assert 'PRIVATE' not in html
    again=routes.business_history(unit)
    assert [row['id'] for row in again.messages]==[row['id'] for row in shown.messages]
    assert len(store.rows)==49 and len(store.operations)==1


@pytest.mark.asyncio
async def test_history_origin_and_four_task_projection_is_read_only(unit,monkeypatch):
    import json
    from app import concierge_actions
    store=MemoryDB();monkeypatch.setattr(db,"db",lambda:store)
    monkeypatch.setattr(db,"list_concierge_messages",REAL_LIST)
    monkeypatch.setattr(db,"visible_modules",lambda *a,**k:{"crm"})
    monkeypatch.setattr(__import__('app.guards',fromlist=['x']),"sees_business_wide",lambda ctx:False)
    batch="0f8fad5b-d9cb-469f-a165-70867728950e"
    for origin in ("typed","voice","live","unknown"):
        REAL_ADD("t1","assistant",("Four tasks [Tasks: "+batch+"]") if origin=="live" else "Fixture "+origin,
                 user_id="u1",origin=origin,display_turn_id="turn-"+origin,
                 trail=[{"name":"list_invoices","label":"Checked invoices","detail":"overdue"}])
    tasks=[{"id":"t"+str(i),"user_id":"u1","batch_id":batch,"title":"Fixture "+status,"status":status,
            "outcome":"Unconfirmed" if status=="failed" else status,"proposal_id":"p"+str(i)}
           for i,status in enumerate(["waiting","done","skipped","failed"])]
    monkeypatch.setattr(db,"list_concierge_tasks",lambda *a,**k:tasks)
    def forbidden(*args,**kw):raise AssertionError("History hydration cannot write or execute")
    monkeypatch.setattr(db,"add_concierge_message",forbidden)
    monkeypatch.setattr(concierge_actions,"execute",forbidden)
    before=len(store.operations)
    response=routes.business_history(unit)
    assert {row['origin'] for row in response.messages}=={"typed","voice","live","unknown"}
    assert all(all(entry['name']!="message_origin" for entry in row['trail']) for row in response.messages)
    live_row=next(row for row in response.messages if row['origin']=="live")
    assert len(json.loads(live_row['tasks_json'])['tasks'])==4
    assert "[Tasks:" not in live_row['content']
    assert 'data-cz-tasks-json=' in response.html and 'data-cz-pill' in response.html
    for origin in ('typed','voice','live','unknown'):assert 'data-cz-origin="'+origin+'"' in response.html
    assert 'Unknown origin' in response.html and 'Unconfirmed' in response.html
    again=routes.business_history(unit)
    assert again.model_dump()==response.model_dump() and len(store.operations)==before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["voice","live"])
async def test_voice_origin_captured_before_native_owner_closes(unit,client,monkeypatch,mode):
    from app import concierge as brain
    saved=[]
    monkeypatch.setattr(db,"add_concierge_message",lambda *args,**kw:saved.append(kw))
    monkeypatch.setattr(routes,"_asker",lambda ctx:None)
    monkeypatch.setattr(routes,"_people_note",lambda *args:"")
    monkeypatch.setattr(routes,"_voice_note",lambda *args,**kw:"")
    monkeypatch.setattr(routes.concierge_delegation,"context_note",lambda *args:"")
    monkeypatch.setattr(routes.concierge_actions,"mailbox_refusal",lambda *args,**kw:None)
    owner=session()
    if mode=="live":live._sessions[owner.id]=owner
    async def model(*args,**kw):
        owner.closed=True;live._sessions.pop(owner.id,None)
        return brain.BusinessReply(reply="Verified fixture read.",links=[],action=None,model="fixture",usage={})
    monkeypatch.setattr(brain,"answer_business",model)
    await live.run_backend(unit,"Check invoices",[],"turn-fixture",owner.voice_session)
    assert len(saved)==2 and all(row['origin']==mode and row['display_turn_id']=='turn-fixture' for row in saved)


@pytest.mark.asyncio
async def test_observed_both_speakers_coalesce_only_contiguous_and_exact_input_link_dedups(unit,monkeypatch):
    store=MemoryDB();monkeypatch.setattr(db,"db",lambda:store)
    monkeypatch.setattr(db,"save_native_transcript",REAL_SAVE)
    monkeypatch.setattr(db,"list_concierge_messages",REAL_LIST)
    s=session()
    for text,start,end,event_id,role in [('Hello ',0,4,'a','user'),('Hi',5,9,'b','assistant'),('again',10,14,'c','user'),(' there',15,19,'d','user')]:
        await live.handle_event(s,{**fragment(text,start,end,event_id),'type':'session.input_transcript.delta' if role=='user' else 'session.output_transcript.delta'},unit)
    await live.flush_transcript(s)
    rows=REAL_LIST('t1',user_id='u1',include_shared=False)
    assert [row['content'] for row in rows]==['Hello ','Hi','again there']
    assert all(row['provisional'] and row['origin']=='live' for row in rows)
    assert len(rows[-1]['observation_ids'])==2 and concierge.sanitize_history(rows,16,3000)==[]
    REAL_ADD('t1','user','Hello ',user_id='u1',origin='live',display_turn_id='verified-turn',native_events={'session':s.id,'event_ids':['a']})
    linked=REAL_LIST('t1',user_id='u1',include_shared=False)
    assert len([row for row in linked if row['content']=='Hello '])==1
    assert len(store.rows)==5
    assert REAL_LIST('t1',user_id='u1',include_shared=False,include_native=False)[0]['content']=='Hello '
    # Same words/event ID in another owned session is a distinct observation.
    other=session();other.id='another-owned-session'
    await live.handle_event(other,fragment('Hello ',0,4,'a'),unit);await live.flush_transcript(other)
    assert len([row for row in REAL_LIST('t1',user_id='u1',include_shared=False) if row['content']=='Hello '])==2


@pytest.mark.asyncio
async def test_legacy_archive_volume_does_not_evict_rich_rows_or_guess_old_mode(unit,monkeypatch):
    store=MemoryDB();monkeypatch.setattr(db,'db',lambda:store)
    monkeypatch.setattr(db,'list_concierge_messages',REAL_LIST)
    monkeypatch.setattr(db,'visible_modules',lambda *a,**k:{'crm'})
    monkeypatch.setattr(__import__('app.guards',fromlist=['x']),'sees_business_wide',lambda ctx:False)
    for i in range(55):
        store.rows[str(i)]={'id':str(i),'tenant_id':'t1','user_id':'u1','role':'user' if i%2 else 'assistant',
            'content':'[Voice transcript segment — provisional; playback unverified] historical '+str(i),
            'created_at':f'2026-10-13T10:{i:02d}:00Z','trail':[]}
    store.rows['rich']={'id':'rich','tenant_id':'t1','user_id':'u1','role':'assistant','content':'Old verified answer',
                        'created_at':'2026-10-12T10:00:00Z','trail':[{'name':'list_invoices','label':'Checked invoices','detail':'overdue'}]}
    shown=routes.business_history(unit)
    assert len(shown.messages)==56 and shown.messages[0]['id']=='rich'
    assert shown.messages[0]['origin']=='unknown' and 'Checked invoices' in shown.html
    assert shown.html.count('data-native-segment=')==55 and 'Unknown origin' in shown.html
    assert len((routes.business_history(unit,context_only=True)).messages)==1
    assert store.operations==[]


@pytest.mark.asyncio
async def test_linked_question_retains_observed_display_order_without_changing_saved_time(unit,monkeypatch):
    from app.templating import templates
    store=MemoryDB();monkeypatch.setattr(db,'db',lambda:store)
    monkeypatch.setattr(db,'list_concierge_messages',REAL_LIST)
    monkeypatch.setattr(db,'visible_modules',lambda *a,**k:{'crm'})
    monkeypatch.setattr(__import__('app.guards',fromlist=['x']),'sees_business_wide',lambda ctx:False)
    def row(key,role,content,second,metadata):
        return {'id':key,'tenant_id':'t1','user_id':'u1','role':role,'content':content,
                'created_at':f'2026-10-13T10:00:{second:02d}Z','trail':[dict(name='message_origin',origin='live',**metadata)]}
    store.rows={r['id']:r for r in [
        row('input','user','[Live observed speech — provisional; playback unverified]\nCheck invoices',0,{'session':'owned','event_id':'input-1','ordinal':1,'provisional':True}),
        row('ack','assistant','[Live observed speech — provisional; playback unverified]\nI will check those',1,{'session':'owned','event_id':'output-1','ordinal':2,'provisional':True}),
        row('question','user','Check invoices',10,{'native_events':{'session':'owned','event_ids':['input-1']}}),
        row('answer','assistant','Verified lookup',11,{})]}
    response=routes.business_history(unit)
    assert [r['id'] for r in response.messages]==['question','ack','answer']
    question=response.messages[0]
    assert question['created_at']=='2026-10-13T10:00:10Z' and question['display_created_at']=='2026-10-13T10:00:00Z'
    assert store.rows['question']['created_at']==question['created_at']
    initial=templates.env.get_template('app/_concierge_history.html').render(chat_history=REAL_LIST('t1',user_id='u1',include_shared=False),brief_cog=None)
    assert initial==response.html
    assert initial.index('data-cz-message-id="question"')<initial.index('data-cz-message-id="ack"')
    assert 'data-cz-created="2026-10-13T10:00:00Z"' in initial
    context=routes.business_history(unit,context_only=True)
    assert [r['id'] for r in context.messages]==['question','answer']
    assert context.messages[0]['created_at']=='2026-10-13T10:00:10Z'
    assert store.operations==[]
