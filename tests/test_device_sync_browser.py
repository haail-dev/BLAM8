"""Two independently loaded native DOMs using real routes and disposable SQL."""

from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import threading
import pytest

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

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
    await pc.contentWindow.Blam8KnowledgeGuide.ask('Keep this decision');
    await pc.contentWindow.Blam8DeviceSync.flush();
    await phone.contentWindow.Blam8DeviceSync.refresh();
    if(phone.contentDocument.querySelector('[name=action_one]').value!=='keep')throw Error('Review choice missing');
    if(!phone.contentWindow.Blam8KnowledgeGuide.isCurrent('next-decision'))throw Error('Review resumed at wrong focus');
    if(!messages(phone).includes('First decision kept'))throw Error('Reply was not persisted by server');
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
    [(DRIVER, 6), (CONFLICT_DRIVER, 1)],
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
    # A single invocation at each step, including the shared guide turn.
    assert len(calls) == expected_calls
