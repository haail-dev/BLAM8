"""Accepting setup cards on the open settings page, in headless Chrome.

The page runs the shipped sync client, dirty-save script and setup-card panel
against the real sync wrapper and disposable SQL. Only the settings table and
the card routes are small stand-ins, with neutral values.
"""

import html
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import pytest

from app import device_sync as sync
from tests.test_device_sync import cluster, sync_db, sync_case, sync_app
from tests.test_device_sync_browser import drive_in_chrome, report_route

PAGES = {
    "invoice_settings": ("Getting paid", "/app/invoices/settings"),
    "crm_settings": ("CRM settings", "/app/crm/settings"),
}


@pytest.fixture
def getting_paid(sync_app, tmp_path):
    """The Getting paid page, its setup cards, and a settings row the accept writes."""
    app = sync_app[0]
    completed, reports = report_route(app)
    saved = {"version": 1, "terms": "", "vat": "20", "deposit": True}
    accepted, accepts = set(), []
    routes = APIRouter(route_class=sync.SyncRoute)

    def card(page):
        done = page in accepted
        return {
            "page": page,
            "revision": 1,
            "considered": [],
            "lines": [
                {
                    "id": page + "-line",
                    "name": "setting",
                    "status": "applied" if done else "proposed",
                    "applied_at": "2026-10-22T09:00:00Z",
                    "display": "Proposed value for " + PAGES[page][0],
                    "sources": {"field": {"kind": "owner_said", "reference": "Said in chat"}},
                    "fields": {},
                    "problems": [],
                }
            ],
        }

    def view(page):
        label, href = PAGES[page]
        preview = {
            "settings_version": "0" * 64,
            "changes": [
                {
                    "id": page + "-line",
                    "display": "Proposed value for " + label,
                    "before": {},
                    "after": {},
                    "notes": [],
                }
            ],
        }
        return {"label": label, "href": href, "rows": {page + "-line": preview}, "all": preview}

    @routes.get("/concierge/setup-cards")
    def listing():
        return {
            "cards": [card(page) for page in PAGES],
            "views": {page: view(page) for page in PAGES},
            "options": {},
        }

    @routes.post("/concierge/setup-cards/{page}/accept")
    async def accept(page: str, request: Request):
        accepts.append((page, await request.json()))
        accepted.add(page)
        if page == "invoice_settings":
            # The card route writes the table directly; the open page does not change.
            saved.update(version=saved["version"] + 1, terms="Net 14", vat="0", deposit=False)
        return {"card": {"page": page, "revision": 2}}

    @routes.get("/app/invoices/settings")
    def invoice_page():
        snapshot = html.escape(json.dumps({"invoice": {"version": saved["version"]}}), quote=True)
        return HTMLResponse(
            f"""<!doctype html><html><body><div data-setup-strip></div>
<main><form method="post" action="/app/invoices/settings">
  <input type="hidden" name="csrf_token" value="test-csrf">
  <input type="hidden" name="invoice_snapshot" value="{snapshot}">
  <input name="payment_terms" value="{saved["terms"]}">
  <input name="vat_rate" value="{saved["vat"]}">
  <input type="checkbox" name="deposit_on_confirm" value="1"{" checked" if saved["deposit"] else ""}>
  <div data-save-row><button>Save settings</button></div>
</form></main>
<div id="messages"><div data-bc-role="assistant">Here is what I would set up.</div></div>
<form id="bc-form"><input name="question"></form>
<script src="/static/device_sync.js"></script>
<script src="/static/dirty_save.js"></script>
<script src="/static/setup_cards.js"></script>
<script>new Blam8SetupCards.Panel({{messages:document.getElementById("messages"),form:document.getElementById("bc-form")}});</script>
</body></html>"""
        )

    app.include_router(routes)

    def run(driver):
        @app.get("/browser-test")
        def host():
            response = HTMLResponse(
                '<!doctype html><iframe id="page" src="/app/invoices/settings"></iframe><script>'
                + PRELUDE
                + driver
                + "</script>"
            )
            response.set_cookie("bl_csrf", "test-csrf")
            return response

        drive_in_chrome(app, tmp_path, "/browser-test", completed)
        assert reports[0]["status"] == "PASS", reports[0]["text"]

    return run, accepts, TestClient(app, cookies={"bl_csrf": "test-csrf"})


PRELUDE = r"""
const frame=document.getElementById('page');
const win=()=>frame.contentWindow, doc=()=>frame.contentDocument, sync=()=>win().Blam8DeviceSync;
const pause=ms=>new Promise(r=>setTimeout(r,ms||40));
const until=async(check)=>{for(let n=0;n<200;n++){const found=check();if(found)return found;await pause();}throw Error('Timed out: '+check);};
const field=name=>doc().querySelector('[name="'+name+'"]');
const type=(name,value)=>{const el=field(name);el.value=value;el.dispatchEvent(new (win().Event)('input',{bubbles:true}));};
const acceptAll=label=>until(()=>{
  const heading=Array.from(doc().querySelectorAll('.bc-setup-heading')).find(h=>h.querySelector('strong') && h.querySelector('strong').textContent===label);
  if (!heading) return null;
  const group=heading.parentElement, accept=group.lastElementChild;
  if (!accept.matches('button') || accept.textContent!=='Accept changes') return null;
  if (!group.querySelector('.bc-setup-line')) throw Error('Acceptance shown without any changes');
  return accept;
}).then(button=>button.click());
const openSuggested=async()=>{(await until(()=>doc().querySelector('.bc-setup-dot.suggested'))).click();};
const loaded=async()=>{await until(()=>sync());await sync().ready;};
const report=(status,text)=>fetch('/browser-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:status,text:text})});
const failed=e=>report('FAIL',e.stack+'\n--- page text ---\n'+(doc().body ? doc().body.innerText : ''));
"""

SCENARIO_DRIVER = r"""
(async()=>{
  try{
    await loaded();
    win().marker='before-accept';
    // The owner messages the assistant. Every flow request first stores a
    // capture of the page's untouched form, as in the live run.
    const asked=await sync().request('/concierge/help',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:'Set up Getting paid for me'})});
    if(!asked.ok)throw Error('Assistant request failed: '+asked.status);
    await openSuggested();
    await acceptAll('Getting paid');
    // The accept wrote the table; the panel reloads the page it changed.
    await until(()=>win().marker===undefined && sync());
    await sync().ready;
    if(field('payment_terms').value!=='Net 14')throw Error('Terms show "'+field('payment_terms').value+'" after the accept');
    if(field('vat_rate').value!=='0')throw Error('VAT shows '+field('vat_rate').value+' after the accept');
    if(field('deposit_on_confirm').checked)throw Error('Deposit is still ticked after the accept');
    if(JSON.parse(field('invoice_snapshot').value).invoice.version!==2)throw Error('The page carries a stale concurrency snapshot');
    const row=doc().querySelector('[data-save-row]');
    if(!row.hidden)throw Error('The Save row is showing although nobody edited the form');
    type('payment_terms','Net 30');
    if(row.hidden)throw Error('The Save row did not appear after an edit');
    const posted=new (win().FormData)(field('payment_terms').form);
    if(JSON.parse(posted.get('invoice_snapshot')).invoice.version!==2)throw Error('Save would post a stale snapshot');
    await report('PASS','Accept all reloads Getting paid with the saved values and a saveable form');
  }catch(e){await failed(e);}
})();
"""

OTHER_PAGE_DRIVER = r"""
(async()=>{
  try{
    await loaded();
    win().marker='before-accept';
    type('payment_terms','Net 21');
    await sync().flush();
    await openSuggested();
    await acceptAll('CRM settings');
    await until(()=>Array.from(doc().querySelectorAll('.bc-setup-applied summary')).some(s=>/CRM settings \S+ applied/.test(s.textContent)));
    // A reload, if one were coming, starts right after the accept is recorded.
    await pause(800);
    if(win().marker!=='before-accept')throw Error('Accepting another page reloaded this one');
    if(field('payment_terms').value!=='Net 21')throw Error('The unsaved edit on this page was lost');
    await report('PASS','Accepting a card for another page leaves this page alone');
  }catch(e){await failed(e);}
})();
"""


def test_accept_all_reloads_getting_paid_with_the_saved_values_and_a_saveable_form(
    getting_paid,
):
    run, accepts, _ = getting_paid
    run(SCENARIO_DRIVER)
    assert [page for page, _ in accepts] == ["invoice_settings"]


def test_accepting_a_card_for_another_page_does_not_reload_this_one(getting_paid):
    run, accepts, c = getting_paid
    run(OTHER_PAGE_DRIVER)
    assert [page for page, _ in accepts] == ["crm_settings"]
    payload = c.get("/device-sync/flow").json()["payload"]
    # This page's draft is kept; other devices are still told to reload.
    assert "/app/invoices/settings:0" in payload["form"]
    assert payload["refresh"]
