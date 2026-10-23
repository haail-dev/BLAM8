"""Native two-document Knowledge Apply with device sync; real routes, disposable SQL."""

from copy import deepcopy
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app import (
    db,
    device_sync as sync,
    knowledge_fidelity,
    knowledge_guidance as guidance,
    knowledge_note_edits as edits,
    knowledge_review,
    knowledge_source_evidence as evidence,
    knowledge_source_review as review,
    knowledge_source_routes as routes,
    llm,
    security,
)
from app.auth import TenantCtx, require_tenant
from tests.test_device_sync import Store, cluster, sync_db, sync_case
from tests.test_knowledge_guidance_browser import chromium_binary
from tests.test_knowledge_review import context, entry

ROOT = Path(__file__).resolve().parents[1]
FINAL = "We handle enquiries.\n\nWe review each request.\n\nInclude a previous reference for a repeated enquiry."
REQUEST = "Please remove the final repeated paragraph and keep the rest."
SCRIPTS = ("device_sync.js", "knowledge_source_review.js", "knowledge_guidance.js", "knowledge_onboarding_review.js")


@pytest.fixture
def apply_app(sync_case, monkeypatch):
    """The real source-correction routes behind the sync wrapper, no model."""
    _, tenant, user = sync_case
    ctx = TenantCtx(str(tenant), None, str(user))

    async def auth(request: Request, response: Response):
        return ctx

    notes = [entry(str(i), "Note " + str(i), "Distinct wording " + str(i) + ".") for i in range(1, 5)]
    notes.append(entry("5", "Ongoing service", FINAL))
    review_ctx, review_row = context(notes, brief=notes[0]["content"])
    review_ctx["version"] = "v1"
    review_row["id"] = str(uuid4())
    review_row["payload"]["questions"] = []
    source = evidence.build([{"url": "https://example.test/help", "text": FINAL}])
    review_row["payload"].update(
        source_snapshot=source["id"],
        draft_revision=1,
        source_units=knowledge_fidelity.inventory(source["pages"]),
    )
    overlap = guidance.present(review_ctx, review_row, {"version": "v1", "action_1": "keep"})["focus"]
    assert overlap["kind"] == "overlap"
    match_name = "match_" + overlap["id"]
    counters = {"previews": 0, "revisions": 0}

    class ReviewStore(Store):
        def rpc(self, name, args):
            if name == "checkpoint_knowledge_review":
                assert args['p_expected']==review_ctx['version']
                review_row['payload']['draft_review_choices']=deepcopy(args['p_choices'])
                return SimpleNamespace(execute=lambda:SimpleNamespace(data=deepcopy(review_ctx)))
            if name != "revise_knowledge_review_sources":
                return super().rpc(name, args)
            assert args["p_expected"] == review_ctx["version"] and args["p_review"] == review_row["id"]
            review_row["payload"] = deepcopy(args["p_payload"])
            review_ctx["version"] = "v" + str(review_row["payload"]["draft_revision"])
            counters["revisions"] += 1
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))

    async def answer(tenant_id, question, card, saved):
        counters["previews"] += 1
        scope = card["knowledge_edit_scope"]
        line = next(l for l in card["lines"] if l["id"] == scope["id"])
        raw = {
            "revision": card["revision"],
            "changes": [{"id": scope["id"], "content_hash": evidence.digest(line["fields"]["content"]),
                         "insertions": [], "citations": [], "removals": [scope["paragraphs"][-1]["reference"]]}],
            "overlaps": [], "questions": [], "edit_scope": edits.binding(card),
        }
        return review.validate(card, saved, raw)

    async def no_model(**_kwargs):
        raise AssertionError("This review path must not call a model")

    monkeypatch.setattr(security, "_BUCKETS", {})
    monkeypatch.setattr(sync.settings, "device_sync_enabled", True)
    monkeypatch.setattr(sync.settings, "knowledge_source_review_enabled", True)
    monkeypatch.setattr(sync, "require_tenant", auth)
    monkeypatch.setattr(db, "db", lambda: ReviewStore(sync_case))
    monkeypatch.setattr(db, "get_member", lambda *a, **k: None)
    monkeypatch.setattr(routes.people, "can_do", lambda *a: True)
    monkeypatch.setattr(routes.fairuse, "onboarding_allowed", lambda *a, **k: True)
    monkeypatch.setattr(knowledge_review, "load", lambda tenant_id: review_ctx)
    monkeypatch.setattr(evidence, "load", lambda tenant_id, identity: deepcopy(source))
    monkeypatch.setattr(review, "answer", answer)
    monkeypatch.setattr(llm, "create", no_model)

    app = FastAPI()
    app.dependency_overrides[require_tenant] = auth
    app.include_router(sync.router)
    app.include_router(routes.router)
    pages = APIRouter(route_class=sync.SyncRoute)
    action = "/onboarding/knowledge/review/" + review_row["id"]

    @pages.get("/onboarding/knowledge")
    def page():
        units = review_row["payload"]["source_units"]
        # The shipped review form's decision fields with the same sync and
        # review scripts; templates, auth, models and customer data are excluded.
        html = (
            f'<!doctype html><html><body><main><form method="post" action="{action}" data-knowledge-review="{review_row["id"]}">'
            '<input name="csrf_token" type="hidden" value="test-csrf">'
            f'<input name="version" type="hidden" value="{review_ctx["version"]}">'
            + "".join(
                f'<section data-draft="{n["id"]}"><input name="label_{n["id"]}" value="{escape(n["label"], quote=True)}">'
                f'<textarea name="content_{n["id"]}">{escape(n["content"])}</textarea>'
                f'<input name="action_{n["id"]}" value=""></section>'
                for n in review_row["payload"]["entries"])
            + f'<label><input type="checkbox" name="{match_name}" value="keep_both"> keep both</label>'
            '<input type="hidden" name="source_default" value="">'
            + "".join(f'<input type="hidden" name="source_{u["id"]}" value=""><input type="hidden" name="source_reason_{u["id"]}" value="">' for u in units)
            + '<details data-source-corrections><input data-source-question><button type="button" data-source-ask>Review drafts against source</button>'
            '<p data-source-status></p><div data-source-preview></div></details></form></main><div id="messages"></div>'
            + "".join(f'<script src="/static/{name}"></script>' for name in SCRIPTS)
            + "<script>Blam8DeviceSync.register({busy:()=>!!window.blam8KnowledgeImportBusy,set:messages=>{document.getElementById('messages').textContent=JSON.stringify(messages);}});</script>"
            "</body></html>"
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @pages.get("/static/{filename}")
    def script(filename: str):
        assert filename in SCRIPTS
        return Response((ROOT / "src/app/static" / filename).read_text(encoding="utf-8"), media_type="application/javascript")

    app.include_router(pages)
    return app, review_ctx, review_row, match_name, counters


def synced(client, url, body, operation=None):
    revision = client.get("/device-sync/flow").json()["revision"]
    return client.post(url, json=body, headers={
        "X-CSRF-Token": "test-csrf", "X-Blam8-Operation": operation or str(uuid4()),
        "X-Blam8-Revision": str(revision), "X-Blam8-Channel": "flow"})


def test_apply_records_advanced_version_and_decisions_in_the_shared_checkpoint(apply_app):
    app, review_ctx, review_row, match_name, counters = apply_app
    c = TestClient(app, cookies={"bl_csrf": "test-csrf"})
    action = "/onboarding/knowledge/review/" + review_row["id"]
    form = {"version": "v1", "action_1": "keep", "action_2": "keep", "action_3": "keep", "action_4": "keep", match_name: "keep_both"}
    checkpoint = {"form": {action + ":0": {"action": action, "version": "v1",
                                           "values": {k: [v] for k, v in form.items() if k != "version"}}}}
    assert c.post("/device-sync/flow", json={"operation": str(uuid4()), "revision": 0, "patch": checkpoint},
                  headers={"X-CSRF-Token": "test-csrf"}).status_code == 200
    _, item = guidance.current(review_ctx, review_row, form)
    assert item["kind"] == "note" and item["id"] == "5"
    scope = {"note_id": "5", "focus_hash": guidance.identity(item)}
    preview = synced(c, action + "/corrections", {"version": "v1", "question": REQUEST, "form": form, **scope})
    assert preview.status_code == 200 and preview.json()["changes"][0]["removals"]
    after_preview = c.get("/device-sync/flow").json()["payload"]
    assert after_preview["form"][action + ":0"]["version"] == "v1"
    operation = str(uuid4())
    applied = synced(c, action + "/corrections/apply",
                     {"version": "v1", "review": preview.json()["review"], "owner_request": REQUEST, "form": form, **scope}, operation)
    assert applied.status_code == 200 and applied.json()["version"] == "v2"
    assert review_row["payload"]["draft_revision"] == 2 and counters == {"previews": 1, "revisions": 1}
    stored = c.get("/device-sync/flow").json()["payload"]
    assert stored["refresh"] == operation
    saved = stored["form"][action + ":0"]
    assert saved["version"] == "v2"
    assert saved["values"]["action_1"] == ["keep"] and saved["values"][match_name] == ["keep_both"]
    assert saved["values"]["action_5"] == [""]
    assert "previous reference" not in saved["values"]["content_5"][0]
    assert saved["values"]["content_5"][0] == review_row["payload"]["entries"][4]["content"]
    excluded = [name for name, value in saved["values"].items() if name.startswith("source_") and value == ["exclude"]]
    assert len(excluded) == 1 and saved["values"]["source_reason_" + excluded[0][7:]] == [REQUEST]
    # Replaying the same operation returns its receipt without a second write.
    assert synced(c, action + "/corrections/apply",
                  {"version": "v1", "review": preview.json()["review"], "owner_request": REQUEST, "form": form, **scope}, operation).status_code == 200
    assert counters["revisions"] == 1
    # A stale repeat against the old version is still refused before any write.
    assert synced(c, action + "/corrections/apply",
                  {"version": "v1", "review": preview.json()["review"], "owner_request": REQUEST, "form": form, **scope}).status_code == 409
    assert counters["revisions"] == 1


DRIVER = r"""
(async()=>{
  const result=document.getElementById('result');
  const pause=()=>new Promise(r=>setTimeout(r,40));
  const until=async(check)=>{for(let n=0;n<150;n++){if(check())return;await pause();}throw Error('Timed out: '+check);};
  const pc=document.getElementById('pc'),phone=document.getElementById('phone');
  const ready=async(frame)=>{await until(()=>frame.contentWindow.Blam8DeviceSync);await frame.contentWindow.Blam8DeviceSync.ready;};
  // A reload replaces the frame's document and its marker. A poll already in
  // flight makes refresh() a no-op, so ask again while waiting under load.
  const reloaded=async(frame,marker)=>{for(let n=0;n<300;n++){if(frame.contentWindow.blam8Document!==marker)return;
    if(n%25===24){const sync=frame.contentWindow.Blam8DeviceSync;if(sync)await sync.refresh().catch(function(){});}await pause();}
    throw Error('Timed out waiting for the '+marker+' document to reload: '+JSON.stringify(frame.contentWindow.Blam8DeviceSync.state())+' / '+frame.contentDocument.body.textContent);};
  const field=(frame,name)=>frame.contentDocument.querySelector('[name="'+name+'"]');
  const decisions=frame=>({keeps:[1,2,3,4].map(n=>field(frame,'action_'+n).value).join(),overlap:field(frame,MATCH).checked,
    edited:field(frame,'action_5').value,note:field(frame,'content_5').value,version:field(frame,'version').value});
  const kept=d=>d.keeps==='keep,keep,keep,keep' && d.overlap && d.edited==='';
  try{
    await ready(pc);
    const guide=pc.contentWindow.Blam8KnowledgeGuide;
    let turn=await guide.ask('Show the current note');
    if(turn.focus.id!=='1')throw Error('Review must open at note 1: '+JSON.stringify(turn.focus));
    turn=await guide.ask('Keep this note');
    if(turn.focus.kind!=='overlap')throw Error('Keeping note 1 must reach its overlap: '+JSON.stringify(turn.focus));
    turn=await guide.ask('Keep both notes');
    for(const n of ['2','3','4']){
      if(turn.focus.id!==n)throw Error('Expected note '+n+': '+JSON.stringify(turn.focus));
      turn=await guide.ask('Keep this note');
    }
    if(turn.focus.kind!=='note' || turn.focus.id!=='5')throw Error('Guide must reach note 5: '+JSON.stringify(turn.focus));
    await pc.contentWindow.Blam8DeviceSync.flush();
    // Open the second device from the checkpoint before Apply. Starting it
    // earlier lets prior guide refreshes race this test's Apply reload marker.
    phone.src='/onboarding/knowledge';
    await ready(phone);
    if(!kept(decisions(phone)))throw Error('Decisions did not reach the phone before Apply: '+JSON.stringify(decisions(phone)));
    const win=pc.contentWindow, nativeFetch=win.fetch.bind(win), form=pc.contentDocument.querySelector('[data-knowledge-review]');
    const shared=win.Blam8DeviceSync.state();
    await win.Blam8KnowledgeSourceReview.ask(REQUEST,form,{note_id:'5',focus_hash:turn.focus_hash});
    const button=pc.contentDocument.querySelector('[data-source-apply]');
    if(!button || !form.querySelector('[data-source-preview]').textContent.includes('Remove this exact paragraph'))throw Error('Removal preview did not offer Apply');
    // Live ordering: the shared revision changes before this device reads the
    // Apply response, and an idle poll compares it with the device's own copy.
    let polled=false;
    win.fetch=async function(url,options){
      const response=await nativeFetch(url,options);
      if(String(url).endsWith('/corrections/apply')){await win.Blam8DeviceSync.refresh();polled=true;}
      return response;
    };
    win.blam8Document='original';
    // Mark the old phone document before Apply: its normal poll may already
    // reload it while the originating device's assertions are running.
    phone.contentWindow.blam8Document='stale';
    button.click();
    await until(()=>pc.contentWindow.blam8Document!=='original' || (polled && !win.Blam8KnowledgeSourceReview.working()));
    if(pc.contentWindow.blam8Document!=='original')throw Error('Originating device reloaded on its own Apply');
    win.fetch=nativeFetch;
    await win.Blam8DeviceSync.refresh();await win.Blam8DeviceSync.refresh();
    if(pc.contentWindow.blam8Document!=='original')throw Error('Originating device reloaded after its own Apply');
    const status=form.querySelector('[data-source-status]').textContent;
    if(!/applied to drafts/i.test(status))throw Error('Apply did not complete: '+status);
    const after=decisions(pc);
    if(!kept(after))throw Error('Apply discarded unsaved decisions: '+JSON.stringify(after));
    if(after.note.includes('previous reference') || !after.note.includes('We review each request.'))throw Error('Removal not applied: '+after.note);
    if(after.version!=='v1')throw Error('Hidden review version must wait for Check changes and overlaps: '+after.version);
    if(form.dataset.syncVersion!=='v2')throw Error('Advanced stored version not recorded for the checkpoint: '+form.dataset.syncVersion);
    await win.Blam8DeviceSync.flush();
    const state=win.Blam8DeviceSync.state();
    if(!state.payload.refresh || state.payload.refresh===shared.payload.refresh)throw Error('Apply left no refresh hint for other devices');
    const snapshot=Object.values(state.payload.form).find(f=>f.action.endsWith('/'+REVIEW));
    if(!snapshot || snapshot.version!=='v2' || snapshot.values.action_1[0]!=='keep' || (snapshot.values[MATCH]||[])[0]!=='keep_both' || snapshot.values.content_5[0].includes('previous reference'))throw Error('Checkpoint lacks the applied draft or decisions: '+JSON.stringify(snapshot));
    if(state.payload.guide.data.focus.id!=='5')throw Error('Shared guide focus moved: '+JSON.stringify(state.payload.guide.data.focus));
    // The second device reloads to the stored drafts and recovers the decisions.
    await phone.contentWindow.Blam8DeviceSync.refresh();
    await reloaded(phone,'stale');
    await ready(phone);
    const restored=decisions(phone);
    if(restored.version!=='v2')throw Error('Second device did not reload to the stored version: '+restored.version);
    if(!kept(restored) || restored.note.includes('previous reference'))throw Error('Second device lost decisions on reload: '+JSON.stringify(restored));
    const resumed=await phone.contentWindow.Blam8KnowledgeGuide.ask('Show the current note');
    if(resumed.focus.kind!=='note' || resumed.focus.id!=='5')throw Error('Guide restarted at '+JSON.stringify(resumed.focus));
    // A stale guide turn on the originating device is still refused until Check
    // changes and overlaps; its decisions stay on the page and survive a reload.
    await win.Blam8DeviceSync.refresh();
    let refused='';
    try{await guide.ask('Show the current note');}catch(e){refused=e.message;}
    if(!/Check changes/.test(refused))throw Error('Stale review guard changed: '+refused);
    if(JSON.stringify(decisions(pc))!==JSON.stringify(after))throw Error('Refused turn altered the form');
    pc.contentWindow.location.reload();
    await reloaded(pc,'original');
    await ready(pc);
    const recovered=decisions(pc);
    if(recovered.version!=='v2' || !kept(recovered) || recovered.note.includes('previous reference'))throw Error('Originating device lost decisions on reload: '+JSON.stringify(recovered));
    const again=await pc.contentWindow.Blam8KnowledgeGuide.ask('Show the current note');
    if(again.focus.kind!=='note' || again.focus.id!=='5')throw Error('Guide restarted after reload at '+JSON.stringify(again.focus));
    result.dataset.status='PASS';result.textContent='Own Apply kept the page and decisions; the second device reloaded and recovered them';
  }catch(e){result.dataset.status='FAIL';result.textContent=e.stack;}
  await fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:result.dataset.status,text:result.textContent})});
})();
"""


def test_own_apply_keeps_decisions_and_other_device_reloads(apply_app, tmp_path):
    app, review_ctx, review_row, match_name, counters = apply_app
    completed = threading.Event()
    reports = []

    @app.post("/browser-result")
    async def browser_result(request: Request):
        reports.append(await request.json())
        completed.set()
        return {"ok": True}

    @app.get("/browser-test")
    def driver():
        constants = "const REVIEW=%s,MATCH=%s,REQUEST=%s;" % tuple(
            json.dumps(v) for v in (review_row["id"], match_name, REQUEST))
        response = HTMLResponse(
            '<!doctype html><iframe id="pc" src="/onboarding/knowledge"></iframe>'
            '<iframe id="phone"></iframe><pre id="result">Running</pre><script>'
            + constants + DRIVER + "</script>")
        response.set_cookie("bl_csrf", "test-csrf")
        return response

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def log_message(self, *_args):
            pass

        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            # Each HTTP request gets an isolated ASGI client; all share only SQL.
            with TestClient(app) as c:
                result = c.request(self.command, self.path, content=body, headers=dict(self.headers), follow_redirects=False)
            self.send_response(result.status_code)
            for key, value in result.headers.multi_items():
                if key.lower() not in ("connection", "transfer-encoding"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(result.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = [
            chromium_binary(), "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--no-first-run", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--user-data-dir=" + str(tmp_path / "browser-profile"),
            f"http://127.0.0.1:{server.server_port}/browser-test",
        ]
        # Files avoid Windows child processes inheriting captured pipe handles.
        with (tmp_path / "chrome.log").open("w") as output:
            browser = subprocess.Popen(command, stdout=output, stderr=output)
            try:
                assert completed.wait(60), (
                    "Native browser did not report: " + (tmp_path / "chrome.log").read_text(errors="replace")[-1500:])
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(browser.pid), "/T", "/F"], stdout=output, stderr=output, timeout=10)
                else:
                    browser.terminate()
                browser.wait(timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert reports[0]["status"] == "PASS", reports[0]["text"]
    # One preview and one draft revision; the forced poll replayed nothing.
    assert counters == {"previews": 1, "revisions": 1}
    assert review_row["payload"]["draft_revision"] == 2 and review_ctx["version"] == "v2"
