"""Two independently loaded native DOMs using real routes and disposable SQL."""

from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import threading
from uuid import uuid4
import pytest

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app import device_sync as sync

from tests.test_device_sync import cluster, sync_db, sync_case, sync_app
from tests.test_knowledge_guidance_browser import chromium_binary


DRIVER = r"""
(async()=>{
  const result=document.getElementById('result');
  const pause=()=>new Promise(r=>setTimeout(r,40));
  const until=async(check)=>{for(let n=0;n<150;n++){if(check())return;await pause();}throw Error('Timed out: '+check);};
  const pc=document.getElementById('pc'),phone=document.getElementById('phone');
  const ready=async(frame)=>{await until(()=>frame.contentWindow.Blam8DeviceSync);await frame.contentWindow.Blam8DeviceSync.ready;};
  const change=(frame,name,value)=>{let el=frame.contentDocument.querySelector('[name="'+name+'"]');el.value=value;el.dispatchEvent(new frame.contentWindow.Event('input',{bubbles:true}));};
  const messages=frame=>frame.contentDocument.getElementById('messages').textContent;
  const advance=async(frame,path)=>{frame.contentDocument.querySelector('form').requestSubmit();await until(()=>frame.contentWindow.location.pathname===path);await ready(frame);};
  const follow=async(frame,path)=>{await frame.contentWindow.Blam8DeviceSync.refresh();await until(()=>frame.contentWindow.location.pathname===path);await ready(frame);};
  try{
    await ready(pc);await ready(phone);
    change(pc,'title','Saved on the PC');await pc.contentWindow.Blam8DeviceSync.flush();
    await phone.contentWindow.Blam8DeviceSync.refresh();
    if(phone.contentDocument.querySelector('[name=title]').value!=='Saved on the PC')throw Error('Unfinished form did not sync');
    await pc.contentWindow.Blam8DeviceSync.transcript([{role:'user',content:'From the PC'}]);
    await phone.contentWindow.Blam8DeviceSync.refresh();
    if(!messages(phone).includes('From the PC'))throw Error('Onboarding message missing');
    await advance(phone,'/onboarding/mailbox');await follow(pc,'/onboarding/mailbox');
    await advance(pc,'/onboarding/voice');await follow(phone,'/onboarding/voice');
    await advance(phone,'/onboarding/knowledge');await follow(pc,'/onboarding/knowledge');
    // The actual guide client changes a decision. Its server checkpoint must
    // restore both that field and the next focus on the other device.
    const initial=await pc.contentWindow.Blam8KnowledgeGuide.ask('Show the current note');
    // Force an old idle poll to return while a guide request is in flight.
    // It must not restore the old fields or mark the new local work conflicted.
    const win=pc.contentWindow, nativeFetch=win.fetch.bind(win);
    let releasePoll,releaseGuide,pollRead=false,guideRead=false,holdPoll=true;
    const pollGate=new Promise(r=>releasePoll=r),guideGate=new Promise(r=>releaseGuide=r);
    win.fetch=async function(url,options){
      const response=await nativeFetch(url,options);
      if(String(url)==='/device-sync/flow' && (!options || !options.method) && holdPoll){holdPoll=false;pollRead=true;await pollGate;}
      if(String(url).endsWith('/guide')){guideRead=true;await guideGate;}
      return response;
    };
    const idlePoll=win.Blam8DeviceSync.refresh();await until(()=>pollRead);
    change(pc,'title','Wording prepared while reviewing');
    const proposed=win.Blam8KnowledgeGuide.ask('Keep this note after editing it.');
    await until(()=>guideRead);releasePoll();await idlePoll;
    if(!win.Blam8DeviceSync.isReady())throw Error('Idle poll conflicted with an active guide turn');
    if(pc.contentDocument.querySelector('[name=title]').value!=='Wording prepared while reviewing')throw Error('Idle poll replaced working wording');
    releaseGuide();await proposed;win.fetch=nativeFetch;

    await pc.contentWindow.Blam8DeviceSync.flush();
    await phone.contentWindow.Blam8DeviceSync.refresh();
    const noteChoice=phone.contentDocument.querySelector('[name^=action_]');
    if(noteChoice.value!=='')throw Error('Model proposal became approval on phone');
    let shared=phone.contentWindow.Blam8DeviceSync.state().payload.messages;
    if(shared.some(m=>m.knowledge_card && m.knowledge_card.selected))throw Error('Model proposal created approved history');
    const editing=await pc.contentWindow.Blam8KnowledgeGuide.ask('Edit this note');
    if(editing.recovered_after_error)throw Error('Editor failed to open in native document');
    await pc.contentWindow.Blam8DeviceSync.flush();await phone.contentWindow.Blam8DeviceSync.refresh();
    shared=phone.contentWindow.Blam8DeviceSync.state().payload.messages;
    if(noteChoice.value!=='' || !shared.some(m=>m.knowledge_card && m.knowledge_card.selected==='edit' && m.knowledge_card.state==='editing'))throw Error('Edit did not remain editing-only');
    const kept=await pc.contentWindow.Blam8KnowledgeGuide.ask('Keep this note');
    if(kept.focus.kind!=='overlap')throw Error('Actual guide did not reach overlap');
    const next=await pc.contentWindow.Blam8KnowledgeGuide.ask('Keep both notes');
    await pc.contentWindow.Blam8DeviceSync.flush();
    await phone.contentWindow.Blam8DeviceSync.refresh();
    const checkbox=phone.contentDocument.querySelector('input[type=checkbox]');
    if(!checkbox.checked || checkbox.value!=='keep_both' || new phone.contentWindow.FormData(checkbox.form).get(checkbox.name)!=='keep_both')throw Error('Native checkbox acknowledgement missing');
    if(!phone.contentWindow.Blam8KnowledgeGuide.isCurrent(next.focus_hash,next.batch_id,next.version))throw Error('Versioned review resumed at wrong focus');
    shared=phone.contentWindow.Blam8DeviceSync.state().payload.messages;
    if(!shared.some(m=>m.knowledge_card && m.knowledge_card.selected==='keep' && m.knowledge_card.state==='prepared'))throw Error('Confirmed choice history missing');
    const fees=await phone.contentWindow.Blam8KnowledgeGuide.ask('Keep this note');
    if(fees.patches.action_two!=='keep')throw Error('Native restored FormData rejected by actual guide');
    await phone.contentWindow.Blam8DeviceSync.flush();
    await pc.contentWindow.Blam8DeviceSync.refresh();
    await advance(pc,'/app/diary/settings');await follow(phone,'/app/diary/settings');
    await advance(phone,'/app/');await follow(pc,'/app/');
    // Free navigation in the finished app must not steer the other screen.
    pc.src='/app/other';await until(()=>pc.contentWindow.location.pathname==='/app/other');await ready(pc);
    await phone.contentWindow.Blam8DeviceSync.refresh();
    if(phone.contentWindow.location.pathname!=='/app/')throw Error('Free app navigation was forced');
    await phone.contentWindow.Blam8DeviceSync.transcript([{role:'user',content:'From the phone in the app'}]);
    await pc.contentWindow.Blam8DeviceSync.refresh();
    if(!messages(pc).includes('From the phone in the app'))throw Error('App assistant message missing');
    result.dataset.status='PASS';result.textContent='Forms, messages, review focus, bidirectional steps, tuning, and free navigation passed';
  }catch(e){result.dataset.status='FAIL';result.textContent=e.stack;}
  await fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:result.dataset.status,text:result.textContent})});
})();
"""

CONFLICT_DRIVER = r"""
(async()=>{
  const pause=()=>new Promise(r=>setTimeout(r,30));
  const pc=document.getElementById('pc'),phone=document.getElementById('phone');
  try{
    for(let n=0;n<150 && (!pc.contentWindow.Blam8DeviceSync || !phone.contentWindow.Blam8DeviceSync);n++)await pause();
    await pc.contentWindow.Blam8DeviceSync.ready;await phone.contentWindow.Blam8DeviceSync.ready;
    const field=pc.contentDocument.querySelector('[name=title]');field.value='Unsaved wording on the PC';
    field.dispatchEvent(new pc.contentWindow.Event('input',{bubbles:true}));
    // The phone advances while the PC has an uncheckpointed edit.
    phone.contentDocument.querySelector('form').requestSubmit();
    for(let n=0;n<150 && phone.contentWindow.location.pathname!='/onboarding/mailbox';n++)await pause();
    if(phone.contentWindow.location.pathname!='/onboarding/mailbox')throw Error('Phone did not advance');
    await pc.contentWindow.Blam8DeviceSync.refresh();
    if(pc.contentWindow.location.pathname!='/onboarding/interview' || field.value!=='Unsaved wording on the PC')throw Error('Conflicting local wording was lost');
    let blocked=false;try{await pc.contentWindow.Blam8DeviceSync.flush();}catch(e){blocked=true;}
    if(!blocked)throw Error('Stale work could overwrite the shared session');
    await fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'PASS',text:'Local conflicting wording preserved'})});
  }catch(e){await fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'FAIL',text:e.stack})});}
})();
"""


@pytest.mark.parametrize(
    "browser_driver,expected_calls",
    [(DRIVER, 12), (CONFLICT_DRIVER, 1)],
    ids=["handover", "conflicting-edits"],
)
def test_two_device_onboarding_and_assistant_roundtrip(
    sync_app, tmp_path, browser_driver, expected_calls
):
    app, calls, _ = sync_app
    completed = threading.Event()
    reports = []

    @app.post("/browser-result")
    async def browser_result(request: Request):
        reports.append(await request.json())
        completed.set()
        return {"ok": True}

    @app.get("/browser-test")
    def driver():
        response = HTMLResponse(
            '<!doctype html><iframe id="pc" src="/onboarding/interview"></iframe>'
            '<iframe id="phone" src="/onboarding/interview"></iframe><pre id="result">Running</pre><script>'
            + browser_driver
            + "</script>"
        )
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
                result = c.request(
                    self.command,
                    self.path,
                    content=body,
                    headers=dict(self.headers),
                    follow_redirects=False,
                )
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
            chromium_binary(),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
            "--no-proxy-server",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--user-data-dir=" + str(tmp_path / "browser-profile"),
            f"http://127.0.0.1:{server.server_port}/browser-test",
        ]
        # Files avoid Windows child processes inheriting captured pipe handles.
        with (tmp_path / "chrome.log").open("w") as output:
            browser = subprocess.Popen(command, stdout=output, stderr=output)
            try:
                assert completed.wait(35), (
                    "Native browser did not report: "
                    + (tmp_path / "chrome.log").read_text(errors="replace")[-1500:]
                )
            finally:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                        stdout=output,
                        stderr=output,
                        timeout=10,
                    )
                else:
                    browser.terminate()
                browser.wait(timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert reports[0]["status"] == "PASS", reports[0]["text"]
    # One additional read-only refresh binds the typed request to edited text.
    if browser_driver == DRIVER:
        index=next(i for i,c in enumerate(calls) if isinstance(c,dict) and c.get('question')=='Keep this note after editing it.')
        assert calls[index-1]['question']=='Show the current note'
    # No duplicate approval or save request.
    assert len(calls) == expected_calls


def drive_in_chrome(app, tmp_path, start_path, completed, wait=35):
    """Serve the app on loopback and let headless Chrome run the page at start_path.

    The page reports through a /browser-result route the test registered, which
    sets completed. Each HTTP request gets an isolated ASGI client; all of them
    share only the disposable SQL database.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def log_message(self, *_args):
            pass

        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            with TestClient(app) as c:
                result = c.request(
                    self.command,
                    self.path,
                    content=body,
                    headers=dict(self.headers),
                    follow_redirects=False,
                )
            self.send_response(result.status_code)
            for key, value in result.headers.multi_items():
                if key.lower() not in ("connection", "transfer-encoding"):
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(result.content)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = [
            chromium_binary(),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
            "--no-proxy-server",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--user-data-dir=" + str(tmp_path / "browser-profile"),
            f"http://127.0.0.1:{server.server_port}{start_path}",
        ]
        # Files avoid Windows child processes inheriting captured pipe handles.
        with (tmp_path / "chrome.log").open("w") as output:
            browser = subprocess.Popen(command, stdout=output, stderr=output)
            try:
                assert completed.wait(wait), (
                    "Native browser did not report: "
                    + (tmp_path / "chrome.log").read_text(errors="replace")[-1500:]
                )
            finally:
                # taskkill can stall on a busy headless tree; bound it, then kill.
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                            stdout=output,
                            stderr=output,
                            timeout=5,
                        )
                    except subprocess.TimeoutExpired:
                        pass
                browser.kill()
                browser.wait(timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def report_route(app):
    """Register /browser-result; return the event it sets and the reports it gets."""
    completed, reports = threading.Event(), []

    @app.post("/browser-result")
    async def browser_result(request: Request):
        reports.append(await request.json())
        completed.set()
        return {"ok": True}

    return completed, reports


SNAPSHOT_DRIVER = r"""
(async()=>{
  const pause=()=>new Promise(r=>setTimeout(r,40));
  const until=async(check)=>{for(let n=0;n<150;n++){if(check())return;await pause();}throw Error('Timed out: '+check);};
  const pc=document.getElementById('pc'),phone=document.getElementById('phone');
  const ready=async(frame)=>{await until(()=>frame.contentWindow.Blam8DeviceSync);await frame.contentWindow.Blam8DeviceSync.ready;};
  const value=(frame,name)=>frame.contentDocument.querySelector('[name="'+name+'"]').value;
  const report=(status,text)=>fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:status,text:text})});
  try{
    await ready(pc);await ready(phone);
    // An earlier client stored the page's concurrency inputs with the form.
    // Restoring that draft must keep what each frame was rendered with.
    for(const frame of [pc,phone]){
      if(value(frame,'title')!=='Stored before the update')throw Error('Ordinary field was not restored');
      for(const name of ['settings_snapshot','invoice_snapshot']){
        if(value(frame,name)!==frame.dataset.rendered)throw Error(name+' was restored over the server render');
      }
    }
    const title=pc.contentDocument.querySelector('[name=title]');
    title.value='Typed on the PC';
    title.dispatchEvent(new pc.contentWindow.Event('input',{bubbles:true}));
    await pc.contentWindow.Blam8DeviceSync.flush();
    await phone.contentWindow.Blam8DeviceSync.refresh();
    if(value(phone,'title')!=='Typed on the PC')throw Error('Ordinary field did not sync');
    for(const frame of [pc,phone]){
      for(const name of ['settings_snapshot','invoice_snapshot']){
        if(value(frame,name)!==frame.dataset.rendered)throw Error(name+' changed after a sync');
      }
    }
    await report('PASS','Concurrency inputs stay with the server render on both devices');
  }catch(e){await report('FAIL',e.stack);}
})();
"""


def test_sync_never_stores_or_restores_a_pages_concurrency_snapshot(
    sync_app, tmp_path
):
    app, _, _ = sync_app
    completed, reports = report_route(app)

    @app.get("/app/crm/settings")
    def settings_page(frame: str = "pc"):
        return HTMLResponse(
            "<!doctype html><html><body><div data-setup-strip></div><main>"
            '<form method="post" action="/app/crm/settings">'
            '<input name="csrf_token" type="hidden" value="test-csrf">'
            '<input name="title" value="Rendered">'
            f'<input name="settings_snapshot" type="hidden" value="snapshot-{frame}">'
            f'<input name="invoice_snapshot" type="hidden" value="snapshot-{frame}">'
            '</form></main><script src="/static/device_sync.js"></script></body></html>'
        )

    @app.get("/browser-test")
    def driver():
        response = HTMLResponse(
            '<!doctype html><iframe id="pc" data-rendered="snapshot-pc" src="/app/crm/settings?frame=pc"></iframe>'
            '<iframe id="phone" data-rendered="snapshot-phone" src="/app/crm/settings?frame=phone"></iframe>'
            "<script>" + SNAPSHOT_DRIVER + "</script>"
        )
        response.set_cookie("bl_csrf", "test-csrf")
        return response

    # What the previous client stored: the whole form, concurrency inputs included.
    c = TestClient(app, cookies={"bl_csrf": "test-csrf"})
    seeded = c.post(
        "/device-sync/flow",
        json={
            "operation": str(uuid4()),
            "revision": 0,
            "patch": {
                "path": "/app/crm/settings",
                "form": {
                    "/app/crm/settings:0": {
                        "action": "/app/crm/settings",
                        "version": "",
                        "values": {
                            "title": ["Stored before the update"],
                            "settings_snapshot": ["snapshot-stale"],
                            "invoice_snapshot": ["snapshot-stale"],
                        },
                    }
                },
            },
        },
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert seeded.status_code == 200

    drive_in_chrome(app, tmp_path, "/browser-test", completed)
    assert reports[0]["status"] == "PASS", reports[0]["text"]
    form = c.get("/device-sync/flow").json()["payload"]["form"]
    names = {name for saved in form.values() for name in saved["values"]}
    assert "title" in names
    assert not names & {"settings_snapshot", "invoice_snapshot"}


DIARY_PAGE = "/app/diary/settings"

DIARY_DRIVER = r"""
(async()=>{
  const frame=document.getElementById('page');
  const win=()=>frame.contentWindow, doc=()=>frame.contentDocument, sync=()=>win().Blam8DeviceSync;
  const pause=ms=>new Promise(r=>setTimeout(r,ms||40));
  const until=async(check)=>{for(let n=0;n<200;n++){const found=check();if(found)return found;await pause();}throw Error('Timed out: '+check);};
  const field=name=>doc().querySelector('[name="'+name+'"]');
  const loaded=async()=>{await until(()=>sync());await sync().ready;};
  const report=(status,text)=>fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:status,text:text})});
  // Every named control of every form in main, as it is now.
  const controls=root=>JSON.stringify(Array.from(root.querySelectorAll('main form')).map(f=>
    Array.from(f.elements).filter(e=>e.name && e.name!=='csrf_token').map(e=>e.name+'='+e.value)));
  const stored=async()=>(await (await fetch('/device-sync/flow',{cache:'no-store'})).json()).payload.form||{};
  try{
    await loaded();
    win().marker='before-accept';
    // The owner messages the assistant. Every flow request first stores the
    // untouched page, as in the live run: blank times, availability unset.
    const asked=await sync().request('/concierge/help',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:'Set up Diary settings for me'})});
    if(!asked.ok)throw Error('Assistant request failed: '+asked.status);
    const before=await stored();
    const actions=Object.values(before).map(f=>f.action).sort().join(' ');
    if(actions!=='/app/diary/cancel-all /app/diary/settings/booking-mode /app/settings/scheduling')throw Error('Stored forms: '+actions);
    if(Object.values(before).some(f=>f.page!=='/app/diary/settings'))throw Error('A stored form does not record the page it was captured on');
    if(field('open_time').value!=='' || field('opening_hours_state').value!=='unset')throw Error('The page did not start untouched');
    // The accept writes the settings; nothing on the open page changes.
    const accepted=await sync().request('/concierge/setup-cards/diary_settings/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    if(!accepted.ok)throw Error('Accept failed: '+accepted.status);
    // The panel then reloads the page it changed.
    win().location.reload();
    await until(()=>win().marker===undefined && sync());
    await sync().ready;
    const remaining=Object.values(await stored()).filter(f=>f.page==='/app/diary/settings');
    if(remaining.length)throw Error('The flow still holds '+remaining.length+' form(s) for the accepted page');
    const render=new DOMParser().parseFromString(await (await fetch('/app/diary/settings',{cache:'no-store'})).text(),'text/html');
    if(field('open_time').value!=='09:00' || field('close_time').value!=='16:00')throw Error('Times show "'+field('open_time').value+'"-"'+field('close_time').value+'" after the accept');
    if(field('opening_hours_state').value!=='set')throw Error('Availability shows '+field('opening_hours_state').value+' after the accept');
    if(field('booking_mode').value!=='stay')throw Error('Booking mode shows '+field('booking_mode').value+' after the accept');
    if(controls(doc())!==controls(render))throw Error('The inputs differ from the server render:\n'+controls(doc())+'\n'+controls(render));
    const rows=Array.from(doc().querySelectorAll('[data-save-row]'));
    if(rows.length!==2 || rows.some(row=>!row.hidden))throw Error('A Save row is showing although nobody edited the page');
    const typed=field('open_time');typed.value='10:00';typed.dispatchEvent(new (win().Event)('input',{bubbles:true}));
    if(typed.form.querySelector('[data-save-row]').hidden)throw Error('The Save row did not appear after an edit');
    await report('PASS','Accepting a card reloads Diary settings with the saved values and no stale draft');
  }catch(e){await report('FAIL',e.stack+'\n--- page text ---\n'+(doc().body ? doc().body.innerText : ''));}
})();
"""


def test_diary_settings_shows_the_saved_values_after_a_card_accept_although_its_forms_post_elsewhere(
    sync_app, tmp_path
):
    """Diary settings posts to three paths and none is the page itself.

    The page's forms are stored untouched, a card accept writes the settings,
    and the reload must render them without the older draft laid over them.
    """
    app, _, _ = sync_app
    completed, reports = report_route(app)
    saved = {"open": "", "close": "", "hours": "unset", "mode": "appointment"}
    routes = APIRouter(route_class=sync.SyncRoute)

    @routes.post("/concierge/setup-cards/{page}/accept")
    async def accept(page: str):
        # The card route writes the table directly; the open page does not change.
        saved.update(open="09:00", close="16:00", hours="set", mode="stay")
        return {"card": {"page": page, "revision": 2}}

    app.include_router(routes)

    def chosen(current, value):
        return " selected" if current == value else ""

    def render():
        return f"""<!doctype html><html><body><div data-setup-strip></div><main>
<form method="post" action="/app/diary/settings/booking-mode">
  <input type="hidden" name="csrf_token" value="test-csrf">
  <select name="booking_mode">
    <option value="appointment"{chosen(saved["mode"], "appointment")}>Appointments</option>
    <option value="stay"{chosen(saved["mode"], "stay")}>Arrival to departure</option>
  </select>
  <div data-save-row><button>Save booking mode</button></div>
</form>
<form method="post" action="/app/settings/scheduling" id="scheduling">
  <input type="hidden" name="csrf_token" value="test-csrf">
  <input type="hidden" name="return_to" value="/app/diary/settings">
  <select name="opening_hours_state">
    <option value="unset"{chosen(saved["hours"], "unset")}>Not set yet</option>
    <option value="set"{chosen(saved["hours"], "set")}>Apply value</option>
  </select>
  <input name="open_time" type="time" value="{saved["open"]}">
  <input name="close_time" type="time" value="{saved["close"]}">
  <div data-save-row><button>Save diary settings</button></div>
</form>
<form method="post" action="/app/diary/cancel-all" id="cancel-all-form">
  <input type="hidden" name="csrf_token" value="test-csrf">
  <input type="hidden" name="return_to" value="/app/diary">
  <button>Cancel all</button>
</form>
</main><script src="/static/device_sync.js"></script><script src="/static/dirty_save.js"></script></body></html>"""

    app.state.pages[DIARY_PAGE] = render

    @app.get("/browser-test")
    def host():
        response = HTMLResponse(
            f'<!doctype html><iframe id="page" src="{DIARY_PAGE}"></iframe><script>'
            + DIARY_DRIVER
            + "</script>"
        )
        response.set_cookie("bl_csrf", "test-csrf")
        return response

    drive_in_chrome(app, tmp_path, "/browser-test", completed)
    assert reports[0]["status"] == "PASS", reports[0]["text"]
