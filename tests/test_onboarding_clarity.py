"""Onboarding clarity (Kris 2026-09-13, after the first real kennels sign-up).

Three things the LittleDogs visit showed: "account creation" and "connect
your mailbox" read as one thing, so a personal Hotmail got connected; the
website read was a box on the page the owner never used (they typed prices
by hand that the site carried); and the assistant's first message asked a
question without saying what the four steps were or what to tell it.

Pins:
- the sign-in page says login and customer inbox are two steps, above the
  buttons; require_tenant stashes the login address for display;
- the mailbox step names the login, asks "same address or another?", flags a
  personal-provider inbox once connected, and ?change=1 re-offers providers;
- the interview captures `website` (template + POST + co-pilot fields + JS,
  the lockstep set) and cleans it;
- the Knowledge step reads the site on arrival (once per session), holds the
  chat interview until the read has finished, and never asks "do you have a
  website?" when one is on file;
- the first greeting lists the four steps and the login-vs-inbox distinction.

Run: python -m pytest tests/test_onboarding_clarity.py -q
"""
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import auth, onboarding
from app import knowledge as kb
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture
def client():
    def _ctx(request: Request):
        # what the real require_tenant stashes: the address they signed in with
        request.state.user_email = "tallyandmicky@gmail.com"
        return TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = _ctx
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


# ── sign-in: two accounts, two jobs ──────────────────────────────────────────
def test_sign_in_page_says_login_and_customer_inbox_are_two_steps():
    html = _read("src/app/templates/login.html")
    assert "Your BLAM8 login" in html and "The inbox your customers email" in html
    assert "We don't read any mail from it" in html
    assert "same address as your login or a different one" in html
    assert "Step 1 · choose your login" in html
    # said above the buttons, not as small print under them
    assert html.index("The inbox your customers email") < html.index("/auth/sso/azure")
    assert "Use whichever account you like" not in html


def test_require_tenant_stashes_the_login_address_for_display():
    src = _read("src/app/auth.py")
    assert 'request.state.user_email = (user.get("email") or "").strip().lower()' in src


def test_consumer_address_helper():
    assert auth.is_consumer_address("MummyToMillie@Hotmail.com")
    assert auth.is_consumer_address("x@hotmail.co.uk")
    assert auth.is_consumer_address("x@btinternet.com")
    assert not auth.is_consumer_address("daycare@littledogs.co.uk")
    assert not auth.is_consumer_address("") and not auth.is_consumer_address(None)
    assert not auth.is_consumer_address("gmail.com")           # no local part


# ── wizard shell: who's signed in, which step, what's next ───────────────────
def test_wizard_header_names_the_signed_in_account_and_the_step_on_phones():
    html = _read("src/app/templates/onboarding/base.html")
    assert "request.state.user_email" in html and "data-signed-in-as" in html
    assert "'mailbox':'Customer inbox'" in html
    line = html.split("data-step-line")[0][-400:]
    assert "hidden" not in line                               # the line shows at every width
    assert "then {{ labels[nxt] }}" in html


def test_wizard_bar_spans_full_width_beside_the_chat_panel():
    """The shared dock compensates the bar while content reserves its column."""
    html = _read("src/app/templates/onboarding/base.html")
    bar = html.split('<header ')[1].split('</header>')[0]
    assert 'data-bc-full-width' in bar and 'data-pull-bar' in bar
    assert 'max-w-2xl' not in bar and 'justify-between' in bar
    assert 'data-signed-in-as' in bar and 'href="/logout"' in bar
    assert 'truncate' in bar and 'flex-none text-sm' in bar
    assert 'height: calc(3rem + env(safe-area-inset-top, 0px));' in html
    shared = _read('src/app/templates/_concierge.html')
    assert '[data-bc-full-width]' in shared and 'margin-right: calc(-1 * clamp(' in shared
    assert 'body:has(#bc-panel' not in html  # reserve stays in the shared dock


# ── mailbox step ─────────────────────────────────────────────────────────────
def _mailbox_env(monkeypatch, connected: str | None):
    monkeypatch.setattr(onboarding.db, "get_tenant",
                        lambda tid, client=None: {"mailbox": connected, "provider": "microsoft"})
    monkeypatch.setattr(onboarding.db, "get_connection_status",
                        lambda tid, client=None: {"ok": True} if connected else None)


def test_mailbox_step_names_the_login_and_asks_which_inbox(client, monkeypatch):
    _mailbox_env(monkeypatch, None)
    r = client.get("/onboarding/mailbox")
    assert r.status_code == 200
    assert "Connect the inbox your customers email" in r.text
    assert 'data-signer-email>tallyandmicky@gmail.com' in r.text
    assert "we don't read any mail from it" in r.text
    assert "connect that same account" in r.text            # same address is a fine answer
    assert "Where is that inbox?" in r.text
    assert "data-personal-inbox" not in r.text              # nothing connected yet
    assert 'href="/oauth/connect?provider=microsoft"' in r.text
    assert "Connect with Google" in r.text and "personal or Google Workspace" not in r.text


def test_connected_personal_inbox_is_asked_about_not_refused(client, monkeypatch):
    # login tallyandmicky@gmail.com, connected a DIFFERENT personal address
    _mailbox_env(monkeypatch, "mummytomillieandella@hotmail.com")
    r = client.get("/onboarding/mailbox")
    # step 2 ticks with the connected address, like step 1 (Kris 2026-09-13)
    assert "data-connected-inbox>mummytomillieandella@hotmail.com</span>. Connected." in r.text
    assert "data-personal-inbox" in r.text
    assert "Is this where your customers' enquiries arrive?" in r.text
    assert "is a hotmail.com address" in r.text              # says which kind, not "personal"
    assert "data-same-as-login" not in r.text
    assert 'href="/onboarding/mailbox?change=1"' in r.text   # not straight into Microsoft
    assert "data-onb-continue" in r.text                     # still free to continue


def _same_as_login(monkeypatch, address: str) -> str:
    def _ctx(request: Request):
        request.state.user_email = address
        return TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = _ctx
    try:
        _mailbox_env(monkeypatch, address)
        return TestClient(app).get("/onboarding/mailbox").text
    finally:
        app.dependency_overrides.clear()


def test_connected_inbox_same_as_login_says_so_not_personal(monkeypatch):
    """Kris, live on step 2 (2026-09-13): business.blam8@gmail.com was both
    the login and the inbox, and "is a personal address" read wrong — the
    note must say WHY it is shown: the same address as the login."""
    html = _same_as_login(monkeypatch, "business.blam8@gmail.com")
    assert "Connected — the same address as your login." in html
    assert "data-same-as-login" in html and "Same address as your login." in html
    assert "data-personal-inbox" not in html                 # same-as-login wins
    assert "personal address" not in html
    # a business domain, same as login: the same calm note, no domain talk
    html = _same_as_login(monkeypatch, "daycare@littledogs.co.uk")
    assert "data-same-as-login" in html and "data-personal-inbox" not in html


def test_mailbox_message_mentions_the_invite_code_only_when_the_box_is_there():
    js = _read("src/app/static/concierge.js")
    assert "var GATE_BOX = !!document.querySelector('form[action=\"/oauth/google-invite\"]');" in js
    assert 'var MAILBOX_INVITE = GATE_BOX' in js
    block = js.split("var ONB_GREETINGS = {")[1].split("var GREETING = ")[0]
    assert block.count("+ MAILBOX_INVITE +") == 2            # greeting + hand-off
    assert "enter your invite code on this page or use Request access" not in block


def test_change_re_offers_the_providers_while_connected(client, monkeypatch):
    _mailbox_env(monkeypatch, "mummytomillieandella@hotmail.com")
    r = client.get("/onboarding/mailbox?change=1")
    assert 'href="/oauth/connect?provider=microsoft"' in r.text
    assert "Connecting another account replaces" in r.text
    assert ">Keep mummytomillieandella@hotmail.com<" in r.text
    # ?change with nothing connected is just the normal page
    _mailbox_env(monkeypatch, None)
    r = client.get("/onboarding/mailbox?change=1")
    assert "Connecting another account replaces" not in r.text


# ── interview: the website is captured at step 1 ─────────────────────────────
def test_website_is_a_lockstep_interview_field():
    from app import concierge
    assert "website" in onboarding.INTERVIEW_CFG_FIELDS
    assert "website" in concierge.ONBOARDING_STEPS["interview"]["fields"]
    assert "whether they have a website" in concierge.ONBOARDING_STEPS["interview"]["guidance"]
    js = _read("src/app/static/concierge.js")
    fields = js.split("var STEP_FIELDS = {")[1].split("};")[0]
    assert '"website"' in fields.split("interview:")[1].split("]")[0]
    html = _read("src/app/templates/onboarding/interview.html")
    assert 'id="website" name="website"' in html
    assert "Step 1 of 4 here" in html and "five short tuning steps" in html


@pytest.mark.parametrize("raw, want", [
    ("littledogs.co.uk", "littledogs.co.uk"),
    (" www.littledogs.co.uk/ ", "www.littledogs.co.uk"),
    ("https://littledogs.co.uk/", "https://littledogs.co.uk"),
    ("none", None),
    ("no website yet", None),
    ("daycare@littledogs.co.uk", None),
    ("", None),
])
def test_clean_website(raw, want):
    assert onboarding._clean_website(raw) == want


def test_interview_post_saves_the_website(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(onboarding.db, "set_tenant_name", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "set_tenant_industry", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: saved.update(cfg))
    r = client.post("/onboarding/interview",
                    data={"business_name": "LittleDogs", "industry": "other",
                          "industry_other": "Dog daycare", "business_brief": "Daycare and boarding",
                          "goals": "", "website": " www.littledogs.co.uk "},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/mailbox"
    assert saved["website"] == "www.littledogs.co.uk"
    assert saved["business_brief"] == "Daycare and boarding"


# ── knowledge: the site on file is read on arrival ───────────────────────────
def _knowledge_env(monkeypatch, website: str | None, sources: list[str]):
    from app import knowledge_review
    monkeypatch.setattr(knowledge_review,"load",lambda tid:{"version":"fixture","brief":"","reviews":[],
        "sources":[{"source":s,"content":"","mailbox_id":None,"chunks":1} for s in sources]})
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    monkeypatch.setattr(onboarding.db, "list_knowledge_sources",
                        lambda tid, client=None: [{"source": s, "chunks": 1} for s in sources])
    monkeypatch.setattr(onboarding.db, "knowledge_source_texts",
                        lambda tid, client=None: [{"source": s, "text": ""} for s in sources])
    monkeypatch.setattr(onboarding.db, "get_tenant_config",
                        lambda tid, client=None: {"website": website})


def test_knowledge_reads_the_step_one_site_on_arrival(client, monkeypatch):
    _knowledge_env(monkeypatch, "littledogs.co.uk", [])
    r = client.get("/onboarding/knowledge")
    assert r.status_code == 200
    assert 'data-auto-site="1"' in r.text
    assert 'data-onb-site="littledogs.co.uk"' in r.text
    assert 'value="littledogs.co.uk"' in r.text               # the box is pre-filled
    assert "Reading your website" in r.text
    # the chat interview waits for the read — and never asks for the site again
    assert 'data-onb-autoask-after="site"' in r.text
    ask = r.text.split('data-onb-autoask="')[1].split('"')[0]
    assert "Start from your website" not in ask


def test_knowledge_does_not_re_read_a_site_already_saved(client, monkeypatch):
    _knowledge_env(monkeypatch, "littledogs.co.uk", ["Website — Prices", "Website — Hours"])
    r = client.get("/onboarding/knowledge")
    assert 'data-auto-site="1"' not in r.text
    assert 'data-onb-site=""' in r.text
    assert 'data-onb-autoask-after' not in r.text              # interview starts straight away
    # done = one line, the box collapsed behind Read again (Kris 2026-09-13)
    assert 'id="site-done"' in r.text and "2 notes saved" in r.text
    assert 'id="site-box"' in r.text and "Start from your website" in r.text
    box_tag = r.text.split('id="site-box"')[1].split(">")[0]
    assert " hidden" in box_tag
    assert 'id="site-again"' in r.text


def test_knowledge_box_is_open_before_any_site_note(client, monkeypatch):
    _knowledge_env(monkeypatch, None, ["Prices"])            # a typed note is not a site read
    r = client.get("/onboarding/knowledge")
    assert 'id="site-done"' not in r.text
    box_tag = r.text.split('id="site-box"')[1].split(">")[0]
    assert " hidden" not in box_tag


def test_knowledge_without_a_site_still_offers_it_by_chat(client, monkeypatch):
    _knowledge_env(monkeypatch, None, [])
    r = client.get("/onboarding/knowledge")
    assert 'data-auto-site="1"' not in r.text
    ask = r.text.split('data-onb-autoask="')[1].split('"')[0]
    assert "Start from your website" in ask                    # nothing on file → ask by chat


def test_the_page_signals_the_read_and_the_panel_waits_for_it():
    html = _read("src/app/templates/onboarding/knowledge.html")
    script=_read("src/app/static/knowledge_onboarding_review.js")
    assert "window.blam8SiteRead={done:true,ok:ok}" in script
    assert "new CustomEvent('blam8:site-read'" in script
    assert "sessionStorage.getItem(stamp)==='1'" in script
    assert "'blam8_site_autoread:'+box.dataset.scope" in script
    assert 'data-scope="{{ (request.state.tenant_id or' in html
    assert "if(done)announce(false);else read();" in script
    js = _read("src/app/static/concierge.js")
    block = js.split("function maybeOnbAutoAsk")[1].split("// ── proactive notifications")[0]
    assert 'el.getAttribute("data-onb-autoask-after") === "site"' in block
    assert 'document.addEventListener("blam8:site-read"' in block
    assert "if (st.ok) return;" in block                        # cards landed → the save reloads
    assert "WEBSITE_SOURCE_PREFIX" in _read("src/app/onboarding.py")


# ── the first message says what is about to happen ───────────────────────────
def test_first_greeting_orients_before_it_asks():
    js = _read("src/app/static/concierge.js")
    intro = js.split("var ONB_INTRO = ")[1].split(";")[0]
    for line in ("1. About you", "2. Your customer inbox", "3. Voice", "4. Knowledge"):
        assert line in intro
    assert "separate from the account you just signed in with" in intro
    # honest count (Kris 2026-09-13): four here, five more in the app
    assert "five more with me" in intro and "Nine in all" in intro
    assert "Last step here" in js and "Last step:" not in js
    assert "Hit Finish setup" not in js and "After Finish setup" not in js   # one flow of nine
    assert intro.rstrip().endswith('what does your business do, and who are your customers?"')
    assert "AI assistant" in intro                              # Art 50 disclosure stays first
    assert "interview: ONB_INTRO," in js
    # the knowledge greeting names the site being read when there is one
    assert 'var ONB_SITE = ' in js
    assert 'so I\'m reading it now' in js



# ── knowledge notes are the business's own words ─────────────────────────────
def test_knowledge_notes_are_written_in_the_businesss_own_voice():
    """Kris, live on step 4 (2026-09-13): the website read wrote "The business
    will contact owners…" — a report about the company, not the company. The
    owner reads these as THEIR knowledge and the agent quotes them, so every
    note-writing prompt asks for first person plural; the site reader also
    never merges the given name with the site's, and never carries over
    "book via <other system>" (BLAM8 is the booking route now)."""
    from app import concierge, kb_website, knowledge_coverage as kc
    assert "first person plural" in kb_website.SYSTEM
    assert "never write 'trading as'" in kb_website.SYSTEM
    assert "never carry over instructions to book through another" in kb_website.SYSTEM
    assert "in the business's own voice" in concierge.ONBOARDING_STEPS["knowledge"]["guidance"]
    block = kc.prompt_block(kc.coverage([]))
    assert "IN THE BUSINESS'S OWN VOICE" in block


def test_saving_a_chat_note_card_never_wipes_pending_website_cards():
    """Re-run 2026-09-13: the trial-day card's save refreshed <main> and the 13
    unsaved website review cards vanished."""
    js = _read("src/app/static/concierge.js")
    block = js.split('if (rec.page === "knowledge") {')[-1].split('if (rec.page === "crm_settings")')[0]
    assert "refreshMainInPlace();" not in block
    assert "The review-fragment event preserves existing form edits" in block


def test_knowledge_step_carries_no_switcher_pointer(client, monkeypatch):
    """The "Coming from Xero?" card sat on the wizard's Knowledge step (P7 of
    docs/onboarding-review-2026-09-30.md): a brand on a first-run screen, and
    an import nobody does mid-setup. It lives under Settings → Bring your data."""
    from pathlib import Path
    tpl = Path("src/app/templates/onboarding/knowledge.html").read_text(encoding="utf-8")
    assert "Xero" not in tpl
    assert "Bring your data" not in tpl


def test_interview_saves_owner_description_verbatim(client, monkeypatch):
    saved = []
    monkeypatch.setattr(onboarding.db, "set_tenant_industry", lambda tid, value, **kw: saved.append(value))
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda *a, **kw: None)
    words = "Repairs, care & custom work for our neighbours"
    response = client.post("/onboarding/interview", data={"industry": words}, follow_redirects=False)
    assert response.status_code == 303
    assert saved == [words]
    template = _read("src/app/templates/onboarding/interview.html")
    assert '<input id="industry"' in template
    assert '<select id="industry"' not in template and 'name="industry_other"' not in template
