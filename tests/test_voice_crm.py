"""Voice CRM — Kris's cold-call debrief tool (voice-crm-build-spec.md).

Pins: Maps-link parsing across the share-link redirect shapes (marker coords
beat the viewport, consent interstitial unwrapped, comma segment → name +
address), extraction clamping (unknown outcome/status/date never reach the
tables raw), the missing-OPENAI_API_KEY refusal, and the /admin/voice-crm
routes end-to-end with the db + LLM seams faked — approve creates prospect +
call, a prospect_id attaches instead of duplicating, review-before-save means
the debrief endpoint writes NOTHING.

Run: python -m pytest tests/test_voice_crm.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.admin as admin
from app import voice_crm
from app.admin_auth import AdminCtx, require_admin
from app.config import settings
from app.main import app
from app.security import require_csrf


# ── Maps URL parsing (pure) ──────────────────────────────────────────────────
def test_parse_place_with_marker_coords_beats_viewport():
    out = voice_crm.parse_maps_url(
        "https://www.google.com/maps/place/Deane+%26+Sons/@53.1,-1.4,17z/"
        "data=!3m1!4b1!8m2!3d53.12345!4d-1.45678")
    assert out["business_name"] == "Deane & Sons"
    assert out["lat"] == 53.12345 and out["lng"] == -1.45678


def test_parse_place_viewport_only():
    out = voice_crm.parse_maps_url("https://www.google.com/maps/place/The+Corner+Cafe/@52.9,-1.1,15z/")
    assert out["business_name"] == "The Corner Cafe"
    assert out["lat"] == 52.9 and out["lng"] == -1.1


def test_parse_place_segment_with_address_splits_name():
    out = voice_crm.parse_maps_url(
        "https://www.google.com/maps/place/Joe's+Plumbing,+12+High+St,+Derby+DE1+1AA/@52.9,-1.4,17z/")
    assert out["business_name"] == "Joe's Plumbing"
    assert out["address"] == "Joe's Plumbing, 12 High St, Derby DE1 1AA"


def test_parse_consent_interstitial_unwraps_continue():
    inner = "https://www.google.com/maps/place/Acme+Ltd/@51.5,-0.1,17z/"
    out = voice_crm.parse_maps_url(
        "https://consent.google.com/m?continue=" + inner.replace(":", "%3A").replace("/", "%2F"))
    assert out["business_name"] == "Acme Ltd"


def test_parse_q_param_format():
    out = voice_crm.parse_maps_url("https://maps.google.com/maps?q=Bob's+Bakery")
    assert out["business_name"] == "Bob's Bakery"


def test_parse_apple_place_link():
    out = voice_crm.parse_apple_maps_url(
        "https://maps.apple.com/place?place-id=IABC123&name=Evercreech+Plumbing"
        "&address=1+High+St,+Evercreech,+Shepton+Mallet+BA4+6JF&coordinate=51.1489,-2.5350")
    assert out["business_name"] == "Evercreech Plumbing"
    assert out["address"] == "1 High St, Evercreech, Shepton Mallet BA4 6JF"
    assert out["lat"] == 51.1489 and out["lng"] == -2.5350


def test_parse_apple_ll_q_form():
    out = voice_crm.parse_apple_maps_url("https://maps.apple.com/?ll=51.14,-2.53&q=Village+Plumber")
    assert out["business_name"] == "Village Plumber"
    assert out["lat"] == 51.14 and out["lng"] == -2.53


@pytest.mark.asyncio
async def test_resolve_rejects_non_maps_hosts():
    with pytest.raises(voice_crm.VoiceCrmError):
        await voice_crm.resolve_maps_link("https://evil.example.com/maps/place/x")


@pytest.mark.asyncio
async def test_resolve_dispatches_apple_hosts_to_apple_parser(monkeypatch):
    """An apple share link that redirects within Apple's domains lands in the
    query-param parser, not the Google path regexes."""
    class FakeResp:
        url = "https://maps.apple.com/place?name=Evercreech+Plumbing&coordinate=51.1,-2.5"

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw): return FakeResp()

    monkeypatch.setattr(voice_crm.httpx, "AsyncClient", lambda **kw: FakeClient())
    out = await voice_crm.resolve_maps_link("https://maps.apple/p/AbC123")
    assert out["business_name"] == "Evercreech Plumbing"
    assert out["lat"] == 51.1 and out["lng"] == -2.5


# ── Extraction clamping ──────────────────────────────────────────────────────
def test_clean_extraction_clamps_unknown_values():
    out = voice_crm.clean_extraction({
        "outcome": "shouted at me", "status": "MAYBE", "summary": " ok ",
        "next_action_date": "next Thursday", "contact_name": ""})
    assert out["outcome"] == "decision_maker"          # unknown → safe default
    assert out["status"] == "CALLED"
    assert out["next_action_date"] is None             # not ISO → dropped
    assert out["summary"] == "ok"
    assert out["contact_name"] is None


def test_clean_extraction_passes_valid_values():
    out = voice_crm.clean_extraction({
        "outcome": "callback_arranged", "status": "CALLBACK", "summary": "s",
        "next_action_date": "2026-08-06", "contact_name": "Dave",
        "phone": "01332 123456"})
    assert out["outcome"] == "callback_arranged" and out["status"] == "CALLBACK"
    assert out["next_action_date"] == "2026-08-06"
    assert out["contact_name"] == "Dave" and out["phone"] == "01332 123456"


@pytest.mark.asyncio
async def test_transcribe_without_key_refuses_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(voice_crm.VoiceCrmError, match="OPENAI_API_KEY"):
        await voice_crm.transcribe(b"audio-bytes")


# ── Routes (db + LLM seams faked) ────────────────────────────────────────────
PROSPECT = {"id": "p-1", "business_name": "Deane & Sons", "status": "CALLBACK",
            "description": None, "location": "Derby", "address": None,
            "phone": None, "website": None, "maps_url": "https://g/1",
            "lat": 53.1, "lng": -1.4, "contact_name": "Dave",
            "next_action": "call back", "next_action_date": "2026-08-06",
            "notes": None, "created_at": "2026-08-03T10:00:00+00:00",
            "updated_at": "2026-08-03T10:00:00+00:00"}


@pytest.fixture
def client():
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[require_admin] = lambda: AdminCtx(
        user_id="u-1", email="kris@blam8.com", role="owner")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_page_renders(client, monkeypatch):
    monkeypatch.setattr(admin.db, "vcrm_list_prospects", lambda status=None, kind=None: [PROSPECT])
    r = client.get("/admin/voice-crm")
    assert r.status_code == 200
    assert "Deane &amp; Sons" in r.text and "CALLBACK" in r.text
    assert 'href="/admin/voice-crm/new"' in r.text


def test_new_page_prefills_existing_prospect(client, monkeypatch):
    monkeypatch.setattr(admin.db, "vcrm_get_prospect",
                        lambda pid: PROSPECT if pid == "p-1" else None)
    r = client.get("/admin/voice-crm/new?prospect=p-1")
    assert r.status_code == 200
    assert 'value="p-1"' in r.text and "Deane &amp; Sons" in r.text


def test_debrief_transcribes_extracts_and_writes_nothing(client, monkeypatch):
    writes = []
    monkeypatch.setattr(admin.db, "vcrm_create_prospect",
                        lambda f: writes.append(f) or PROSPECT)
    monkeypatch.setattr(admin.db, "vcrm_create_call",
                        lambda *a, **k: writes.append(a) or {})

    async def fake_transcribe(data, filename="x", content_type="y"):
        return "spoke to dave, call back thursday"

    async def fake_extract(transcript, prospect=None):
        return {"fields": voice_crm.clean_extraction(
            {"outcome": "callback_arranged", "status": "CALLBACK", "summary": "call back"}),
            "raw": {"outcome": "callback_arranged"}}

    monkeypatch.setattr(admin.voice_crm, "transcribe", fake_transcribe)
    monkeypatch.setattr(admin.voice_crm, "extract_debrief", fake_extract)
    r = client.post("/admin/voice-crm/debrief",
                    files={"audio": ("debrief.webm", b"\x1aE...", "audio/webm")},
                    data={"prospect_id": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"].startswith("spoke to dave")
    assert body["fields"]["status"] == "CALLBACK"
    assert writes == []        # review-before-save: the debrief endpoint stores nothing


def test_approve_creates_prospect_and_call(client, monkeypatch):
    created = {}
    monkeypatch.setattr(admin.db, "vcrm_get_prospect", lambda pid: None)

    def fake_create(f):
        created["prospect"] = f
        return {**PROSPECT, "id": "p-9"}
    monkeypatch.setattr(admin.db, "vcrm_create_prospect", fake_create)
    monkeypatch.setattr(admin.db, "vcrm_create_call",
                        lambda *a: created.setdefault("call", a) and {})
    r = client.post("/admin/voice-crm/approve", follow_redirects=False, data={
        "business_name": "New Biz", "status": "CALLED", "outcome": "gatekeeper",
        "summary": "gatekeeper took a message", "transcript": "the words",
        "lat": "53.1", "lng": "-1.4", "extraction_json": '{"outcome": "gatekeeper"}'})
    assert r.status_code == 303 and r.headers["location"] == "/admin/voice-crm/p-9"
    assert created["prospect"]["business_name"] == "New Biz"
    assert created["prospect"]["lat"] == 53.1
    pid, outcome, summary, transcript, extraction = created["call"]
    assert pid == "p-9" and outcome == "gatekeeper"
    assert transcript == "the words" and extraction == {"outcome": "gatekeeper"}


def test_approve_with_prospect_id_attaches_not_duplicates(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(admin.db, "vcrm_get_prospect",
                        lambda pid: PROSPECT if pid == "p-1" else None)
    monkeypatch.setattr(admin.db, "vcrm_update_prospect",
                        lambda pid, f: calls.setdefault("update", (pid, f)))
    monkeypatch.setattr(admin.db, "vcrm_create_prospect",
                        lambda f: pytest.fail("must update, not create"))
    monkeypatch.setattr(admin.db, "vcrm_create_call", lambda *a: calls.setdefault("call", a) or {})
    r = client.post("/admin/voice-crm/approve", follow_redirects=False, data={
        "prospect_id": "p-1", "business_name": "Deane & Sons", "status": "INTERESTED",
        "outcome": "decision_maker", "summary": "keen"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/voice-crm/p-1"
    assert calls["update"][0] == "p-1"
    assert calls["update"][1]["status"] == "INTERESTED"


def test_approve_unknown_status_clamped_and_name_required(client, monkeypatch):
    monkeypatch.setattr(admin.db, "vcrm_get_prospect", lambda pid: None)
    seen = {}

    def fake_create(f):
        seen["f"] = f
        return {**PROSPECT, "id": "p-9"}
    monkeypatch.setattr(admin.db, "vcrm_create_prospect", fake_create)
    monkeypatch.setattr(admin.db, "vcrm_create_call", lambda *a: {})
    assert client.post("/admin/voice-crm/approve",
                       data={"business_name": "", "status": "CALLED"}).status_code == 400
    r = client.post("/admin/voice-crm/approve", follow_redirects=False,
                    data={"business_name": "X", "status": "HACKED", "outcome": "nope"})
    assert r.status_code == 303
    assert seen["f"]["status"] == "NEW"                # unknown status never stored raw


def test_prospect_page_renders_calls_and_404s(client, monkeypatch):
    monkeypatch.setattr(admin.db, "vcrm_get_prospect",
                        lambda pid: PROSPECT if pid == "p-1" else None)
    monkeypatch.setattr(admin.db, "vcrm_list_calls", lambda pid: [
        {"id": "c-1", "called_at": "2026-08-03T14:00:00+00:00", "outcome": "decision_maker",
         "summary": "good chat", "transcript": "the full words", "extraction_json": {}}])
    r = client.get("/admin/voice-crm/p-1")
    assert r.status_code == 200
    assert "good chat" in r.text and "the full words" in r.text
    assert "Log a call" in r.text
    assert client.get("/admin/voice-crm/nope").status_code == 404


def test_resolve_endpoint_reports_existing_prospect(client, monkeypatch):
    async def fake_resolve(url):
        return {"business_name": "Deane & Sons", "address": None,
                "lat": 53.1, "lng": -1.4, "maps_url": "https://g/1"}
    monkeypatch.setattr(admin.voice_crm, "resolve_maps_link", fake_resolve)
    monkeypatch.setattr(admin.db, "vcrm_find_prospect", lambda m, b: PROSPECT)
    r = client.post("/admin/voice-crm/resolve", data={"maps_url": "https://maps.app.goo.gl/x"})
    assert r.status_code == 200
    assert r.json()["existing"]["id"] == "p-1"


def test_list_page_carries_the_quick_action_sheet(client, monkeypatch):
    """Press & hold (Kris, 2026-08-03): every card is long-pressable and the
    sheet offers every status + the two-tap delete."""
    monkeypatch.setattr(admin.db, "vcrm_list_prospects", lambda status=None, kind=None: [PROSPECT])
    r = client.get("/admin/voice-crm?status=CALLBACK")
    assert "data-vc-card" in r.text and 'id="vc-sheet"' in r.text
    for s in voice_crm.STATUSES:
        assert f'data-vc-status="{s}"' in r.text
    assert 'id="vc-sheet-delete"' in r.text
    assert 'value="/admin/voice-crm?status=CALLBACK"' in r.text   # back keeps the filter


def test_quick_status_updates_and_clamps(client, monkeypatch):
    updates = []
    monkeypatch.setattr(admin.db, "vcrm_get_prospect",
                        lambda pid: PROSPECT if pid == "p-1" else None)
    monkeypatch.setattr(admin.db, "vcrm_update_prospect",
                        lambda pid, f: updates.append((pid, f)))
    r = client.post("/admin/voice-crm/p-1/status", follow_redirects=False,
                    data={"status": "WON", "back": "/admin/voice-crm?status=CALLBACK"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/voice-crm?status=CALLBACK"
    assert updates == [("p-1", {"status": "WON"})]
    # Unknown status / unknown prospect: redirect, never a write.
    client.post("/admin/voice-crm/p-1/status", follow_redirects=False,
                data={"status": "HACKED"})
    client.post("/admin/voice-crm/nope/status", follow_redirects=False,
                data={"status": "WON"})
    assert len(updates) == 1
    # A form-supplied back path outside Voice CRM must not become a redirect.
    r = client.post("/admin/voice-crm/p-1/status", follow_redirects=False,
                    data={"status": "WON", "back": "https://evil.example.com"})
    assert r.headers["location"] == "/admin/voice-crm"


def test_quick_delete_removes_and_404s_on_missing(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(admin.db, "vcrm_get_prospect",
                        lambda pid: PROSPECT if pid == "p-1" else None)
    monkeypatch.setattr(admin.db, "vcrm_delete_prospect", lambda pid: deleted.append(pid))
    r = client.post("/admin/voice-crm/p-1/delete", follow_redirects=False,
                    data={"back": "/admin/voice-crm"})
    assert r.status_code == 303 and deleted == ["p-1"]
    assert client.post("/admin/voice-crm/nope/delete", data={}).status_code == 404


def test_nav_menu_gains_the_voice_crm_link():
    from app.templating import templates
    html = templates.env.get_template("admin/base.html").render()
    assert 'href="/admin/voice-crm"' in html


# ── Partner pipeline (2026-10-01): kind / role / channel / source / email ────
PARTNER = {**PROSPECT, "id": "p-2", "business_name": "Taunton Books", "status": "IDENTIFIED",
           "kind": "partner", "role": "bookkeeper", "channel": "email", "source": "ICB directory",
           "email": "hello@tauntonbooks.example"}


def test_add_partner_creates_a_partner_row(client, monkeypatch):
    created = {}
    monkeypatch.setattr(admin.db, "vcrm_create_prospect",
                        lambda f: created.setdefault("p", f) and {"id": "p-7"})
    r = client.post("/admin/voice-crm/add", follow_redirects=False, data={
        "business_name": "Taunton Books", "kind": "partner", "role": "bookkeeper",
        "channel": "email", "source": "ICB directory", "email": "hello@tauntonbooks.example"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/voice-crm/p-7"
    f = created["p"]
    assert f["kind"] == "partner" and f["role"] == "bookkeeper" and f["channel"] == "email"
    assert f["status"] == "IDENTIFIED"                       # partner default, not the call default NEW


def test_add_clamps_unknown_kind_and_channel_and_needs_a_name(client, monkeypatch):
    created = {}
    monkeypatch.setattr(admin.db, "vcrm_create_prospect",
                        lambda f: created.setdefault("p", f) and {"id": "p-8"})
    client.post("/admin/voice-crm/add", follow_redirects=False,
                data={"business_name": "X", "kind": "wizard", "channel": "carrier-pigeon"})
    assert created["p"]["kind"] == "customer" and created["p"]["channel"] is None
    r = client.post("/admin/voice-crm/add", follow_redirects=False, data={"business_name": " "})
    assert r.headers["location"] == "/admin/voice-crm/add?err=name"


def test_debrief_approve_does_not_blank_the_partner_fields(client, monkeypatch):
    """The call-debrief review form carries no kind/role/email: updating a prospect from it
    must leave those columns alone, not write NULL over them."""
    updates = []
    monkeypatch.setattr(admin.db, "vcrm_get_prospect", lambda pid: PARTNER)
    monkeypatch.setattr(admin.db, "vcrm_update_prospect", lambda pid, f: updates.append(f))
    monkeypatch.setattr(admin.db, "vcrm_create_call", lambda *a: {})
    client.post("/admin/voice-crm/approve", follow_redirects=False, data={
        "prospect_id": "p-2", "business_name": "Taunton Books", "status": "REPLIED"})
    assert updates and not any(k in updates[0] for k in ("kind", "role", "channel", "source", "email"))


def test_touch_logs_history_and_moves_status(client, monkeypatch):
    calls, ups = [], []
    monkeypatch.setattr(admin.db, "vcrm_get_prospect", lambda pid: PARTNER if pid == "p-2" else None)
    monkeypatch.setattr(admin.db, "vcrm_create_call", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(admin.db, "vcrm_update_prospect", lambda pid, f: ups.append((pid, f)))
    r = client.post("/admin/voice-crm/p-2/touch", follow_redirects=False,
                    data={"summary": "Emailed the intro", "status": "EMAILED"})
    assert r.status_code == 303
    assert calls == [("p-2", None, "Emailed the intro", None, None)]
    assert ups == [("p-2", {"status": "EMAILED"})]
    assert client.post("/admin/voice-crm/nope/touch", data={"summary": "x"}).status_code == 404


def test_list_filters_by_kind_and_shows_partner_chips(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(admin.db, "vcrm_list_prospects",
                        lambda status=None, kind=None: seen.update(kind=kind) or [PARTNER])
    r = client.get("/admin/voice-crm?kind=partner")
    assert seen["kind"] == "partner"
    assert "Taunton Books" in r.text and "bookkeeper" in r.text and "via email" in r.text
    assert "voice-crm?status=EMAILED&amp;kind=partner" in r.text


def test_prospect_page_shows_partner_fields_and_touch_form(client, monkeypatch):
    monkeypatch.setattr(admin.db, "vcrm_get_prospect", lambda pid: PARTNER)
    monkeypatch.setattr(admin.db, "vcrm_list_calls", lambda pid: [])
    r = client.get("/admin/voice-crm/p-2")
    assert r.status_code == 200
    assert 'name="email"' in r.text and "hello@tauntonbooks.example" in r.text
    assert '/admin/voice-crm/p-2/touch' in r.text and "CALL BOOKED" in r.text


def test_extraction_never_picks_a_partner_status():
    assert voice_crm.clean_extraction({"outcome": "interested", "summary": "s", "status": "EMAILED"})["status"] == "CALLED"
