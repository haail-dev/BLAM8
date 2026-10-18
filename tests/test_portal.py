"""Customer Portal (docs/modules/module-portal.md): the public self-service page.

Pins the security model and the plumbing:
  * slug + reference-code minting (unambiguous alphabet, collision retry)
  * the sign-off footer (on/off switch, never raises into a send)
  * /c/{slug}: renders the business, 404s unknown slugs AND switched-off
    portals with the same page
  * tool executor guards: identity-gated reads, ownership check on
    move_booking, booking budget, tenant-scoped reference lookup
  * escalation lands as a needs-attention inbox row
  * portal settings save + slug validation

Run: python -m pytest tests/test_portal.py -q
"""
from app import scheduling
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.portal as portal
import app.portal_data as portal_data
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.portal_data import PortalSession


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _run(coro):
    return asyncio.run(coro)


# ── slug + reference-code minting ────────────────────────────────────────────
def test_slugify_and_prefix():
    assert portal.slugify("Acme Plumbing & Heating") == "acme-plumbing-heating"
    assert portal.slugify("  --Weird__ Name!! ") == "weird-name"
    assert portal.slugify("") == "business"
    assert portal.ref_prefix("Acme Plumbing & Heating") == "ACME"
    assert portal.ref_prefix("360-Degrees Ltd") == "360DEG"
    assert portal.ref_prefix("!!!") == "REF"


def test_mint_code_uses_unambiguous_alphabet():
    for _ in range(50):
        code = portal.mint_code("ACME")
        prefix, suffix = code.split("-")
        assert prefix == "ACME" and len(suffix) == 6   # 6 since the 2026-09 review (H3)
        assert not set(suffix) & set("O0I1")


def test_ensure_reference_mints_once_and_retries_collisions(monkeypatch):
    contact = {"id": "c1", "email": "s@x.com", "reference_code": None}
    attempts = []
    monkeypatch.setattr(portal.db, "crm_get_or_create_contact",
                        lambda *a, **k: dict(contact))
    monkeypatch.setattr(portal.db, "get_tenant", lambda *a, **k: {"name": "Acme Co"})
    # first mint collides, second sticks
    monkeypatch.setattr(portal.db, "set_contact_reference",
                        lambda cid, tid, code: attempts.append(code) or len(attempts) > 1)
    out = portal.ensure_reference("t1", "s@x.com", "Sarah")
    assert out["reference_code"] == attempts[-1]
    assert len(attempts) == 2
    # an existing code is never re-minted
    monkeypatch.setattr(portal.db, "crm_get_or_create_contact",
                        lambda *a, **k: {"id": "c1", "reference_code": "ACME-AAAA"})
    assert portal.ensure_reference("t1", "s@x.com")["reference_code"] == "ACME-AAAA"


def test_ensure_portal_slug_appends_on_collision(monkeypatch):
    monkeypatch.setattr(portal.db, "get_tenant",
                        lambda *a, **k: {"name": "Acme Plumbing", "portal_slug": None})
    claimed = []
    monkeypatch.setattr(portal.db, "set_portal_slug",
                        lambda tid, slug: claimed.append(slug) or len(claimed) > 1)
    assert portal.ensure_portal_slug("t1") == "acme-plumbing-2"
    assert claimed == ["acme-plumbing", "acme-plumbing-2"]
    # an existing slug short-circuits
    monkeypatch.setattr(portal.db, "get_tenant",
                        lambda *a, **k: {"portal_slug": "kept"})
    assert portal.ensure_portal_slug("t1") == "kept"


# ── sign-off footer ──────────────────────────────────────────────────────────
def _footer_env(monkeypatch):
    monkeypatch.setattr(portal.db, "get_tenant",
                        lambda *a, **k: {"name": "Acme Co", "portal_slug": "acme-co"})
    monkeypatch.setattr(portal.db, "crm_get_or_create_contact",
                        lambda *a, **k: {"id": "c1", "reference_code": "ACME-7K3M"})


def test_email_footer_carries_code_and_link(monkeypatch):
    _footer_env(monkeypatch)
    out = portal.email_footer("t1", {"portal_enabled": True}, "s@x.com")
    assert "Your reference: ACME-7K3M" in out
    # A signed link since the 2026-09 review (H3): the code alone opens nothing.
    assert "/c/acme-co?t=ACME-7K3M." in out and "?ref=" not in out
    tok = out.split("?t=")[1].split()[0]
    assert portal.code_from_link("t1", tok) == "ACME-7K3M"
    assert portal.code_from_link("t2", tok) is None                 # another business
    assert portal.code_from_link("t1", "ACME-7K3M.forged") is None


def test_email_footer_respects_the_off_switch_and_missing_cfg_defaults_on(monkeypatch):
    _footer_env(monkeypatch)
    assert portal.email_footer("t1", {"portal_enabled": False}, "s@x.com") == ""
    assert "ACME-7K3M" in portal.email_footer("t1", {}, "s@x.com")   # default ON
    assert portal.email_footer("t1", {}, None) == ""


def test_email_footer_never_raises_into_a_send(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(portal.db, "get_tenant", _boom)
    assert portal.email_footer("t1", {}, "s@x.com") == ""


# ── /c/{slug} page ───────────────────────────────────────────────────────────
def _page_env(monkeypatch, tenant={"id": "t1", "name": "Acme Plumbing",
                                   "portal_slug": "acme-plumbing"},
              cfg={"portal_enabled": True, "portal_description": "Boilers fixed"}):
    monkeypatch.setattr(portal.db, "get_tenant_by_portal_slug",
                        lambda slug: dict(tenant) if slug == tenant.get("portal_slug") else None)
    monkeypatch.setattr(portal.db, "get_tenant_config", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(portal.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(portal.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda *a, **k: None)


def test_portal_page_renders_business(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.get("/c/acme-plumbing")
    assert res.status_code == 200
    assert "Acme Plumbing" in res.text
    assert "Boilers fixed" in res.text
    assert "Powered by" in res.text
    assert "Book a service" in res.text        # diary on → the booking chip shows


def test_unknown_slug_is_a_friendly_404(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.get("/c/nobody-here")
    assert res.status_code == 404
    assert "isn't on BLAM8" in res.text


def test_disabled_portal_404s_identically(client, monkeypatch):
    _page_env(monkeypatch, cfg={"portal_enabled": False})
    res = client.get("/c/acme-plumbing")
    assert res.status_code == 404


def test_ref_param_greets_the_matched_customer_with_their_booking(client, monkeypatch):
    _page_env(monkeypatch)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: {"name": "Sarah Jones",
                                           "email": "s@x.com",
                                           "reference_code": "ACME-7K3M"}
                        if code == "ACME-7K3M" else None)
    monkeypatch.setattr(portal.db, "bookings_for_contact",
                        lambda tid, email, **k: [
                            {"booking_date": "2099-07-31", "window": "am",
                             "summary": "Annual boiler service",
                             "booking_ref": "BK-1001", "status": "confirmed"}])
    res = client.get("/c/acme-plumbing?ref=acme-7k3m")   # case-insensitive
    assert "Hi Sarah" in res.text
    # personalised landing leads with their next booking
    assert "Annual boiler service" in res.text
    assert "in the morning" in res.text and "BK-1001" in res.text
    # …and a customer with nothing upcoming gets the book-one nudge
    monkeypatch.setattr(portal.db, "bookings_for_contact", lambda *a, **k: [])
    res2 = client.get("/c/acme-plumbing?ref=acme-7k3m")
    assert "no upcoming bookings" in res2.text


def test_booking_phrase_formats():
    assert portal._booking_phrase(
        {"summary": "Boiler service", "booking_date": "2099-07-31",
         "window": "am", "booking_ref": "BK-1001"}
    ) == "Boiler service — Friday 31 July in the morning (BK-1001)"
    assert portal._booking_phrase(
        {"summary": "MOT", "booking_date": "2099-08-03", "window": "10:00"}
    ) == "MOT — Monday 3 August at 10:00"
    assert portal._booking_phrase(
        {"summary": "Skip hire", "booking_date": "bad-date", "window": "day"}
    ) == "Skip hire — bad-date"
    # the customer's own name baked into an agent-created summary is stripped
    assert portal._booking_phrase(
        {"summary": "Boiler service — Velox Velcee", "booking_date": "2099-07-31",
         "window": "am", "booking_ref": "BK-1001"}, customer_name="Velox Velcee"
    ) == "Boiler service — Friday 31 July in the morning (BK-1001)"


# ── tool executor guards ─────────────────────────────────────────────────────
def _session(**kw):
    defaults = dict(tenant_id="t1", business_name="Acme", scheduling_on=True)
    defaults.update(kw)
    return PortalSession(**defaults)


def test_identity_gated_tools_refuse_anonymous_sessions(monkeypatch):
    s = _session()
    for tool in ("list_my_bookings", "escalate_to_business",
                 "create_booking", "move_booking"):
        out = _run(portal_data.execute(s, tool, {"message": "hi",
                                                 "booking_date": "2026-08-01",
                                                 "window": "am", "summary": "x",
                                                 "booking_id": "b1",
                                                 "new_date": "2026-08-01",
                                                 "new_window": "am"}))
        assert out["error"] == "not_identified", tool


def test_lookup_reference_is_tenant_scoped_and_sets_identity(monkeypatch):
    s = _session()
    monkeypatch.setattr(
        portal_data.db, "crm_contact_by_reference",
        lambda tid, code: ({"id": "c1", "name": "Sarah", "email": "s@x.com",
                            "reference_code": "ACME-7K3M"}
                           if (tid, code) == ("t1", "ACME-7K3M") else None))
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [
                            {"id": "b1", "booking_ref": "BK-1001",
                             "booking_date": "2099-01-01", "window": "am",
                             "status": "confirmed", "summary": "Service"}])
    # a code from another business never resolves
    out = _run(portal_data.execute(s, "lookup_reference", {"code": "OTHER-XXXX"}))
    assert out["found"] is False and s.contact is None
    out = _run(portal_data.execute(s, "lookup_reference", {"code": "acme-7k3m"}))
    assert out["found"] is True
    assert s.contact["email"] == "s@x.com"
    assert out["upcoming_bookings"][0]["booking_ref"] == "BK-1001"
    # the full code never enters the model's context — masked form only
    import json as _json
    assert "ACME-7K3M" not in _json.dumps(out)
    assert out["customer"]["reference_masked"] == "ACME-7K**"


def test_move_booking_refuses_other_customers_rows(monkeypatch):
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    monkeypatch.setattr(portal_data.db, "get_booking",
                        lambda bid, tid, **k: {"id": bid, "contact": "other@x.com",
                                               "status": "confirmed"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID, "new_date": "2026-08-01",
                                    "new_window": "am"}))
    assert out["error"] == "not_found"


def test_create_booking_respects_the_budget_and_confirms(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        portal_data.scheduling, "place_hold",
        lambda tid, day, window, summary, **kw: (
            {"id": "b1", "booking_ref": "BK-1001", "booking_date": str(day),
             "window": window, "status": "proposed", "summary": summary}, None))
    monkeypatch.setattr(portal_data.db, "set_booking_status",
                        lambda bid, status, tid, **k: calls.setdefault("status", status))
    async def _no_push(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_push)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"},
                 booking_allowed=lambda: True)
    out = _run(portal_data.execute(s, "create_booking",
                                   {"booking_date": "2026-08-01", "window": "am",
                                    "summary": "Boiler service"}))
    assert out["booked"] is True and out["booking"]["booking_ref"] == "BK-1001"
    assert calls["status"] == "confirmed"
    # budget exhausted → polite refusal, no write
    s2 = _session(contact={"id": "c1", "email": "s@x.com"},
                  booking_allowed=lambda: False)
    out2 = _run(portal_data.execute(s2, "create_booking",
                                    {"booking_date": "2026-08-01", "window": "am",
                                     "summary": "x"}))
    assert out2["error"] == "limit_reached"


def test_full_slot_offers_alternatives_not_a_stacktrace(monkeypatch):
    monkeypatch.setattr(portal_data.scheduling, "place_hold",
                        lambda *a, **k: (None, "full"))
    s = _session(contact={"id": "c1", "email": "s@x.com"},
                 booking_allowed=lambda: True)
    out = _run(portal_data.execute(s, "create_booking",
                                   {"booking_date": "2026-08-01", "window": "am",
                                    "summary": "x"}))
    assert out["error"] == "slot_full"


def test_check_availability_carries_day_summary_for_a_period(monkeypatch):
    # Period ask (2026-08-02): multi-day ranges also return per-day counts —
    # computed over the FULL slot list, not the 40-slot cap — so the chat can
    # answer "what's your availability next week?" with honest counts.
    monkeypatch.setattr(
        portal_data.scheduling, "check_availability",
        lambda tid, d1, d2, **k: [
            {"date": "2026-08-04", "window": "10:00", "remaining": 1},
            {"date": "2026-08-04", "window": "14:00", "remaining": 1},
            {"date": "2026-08-05", "window": "10:00", "remaining": 2}])
    s = _session()
    out = _run(portal_data.execute(s, "check_availability",
                                   {"date_from": "2026-08-03", "date_to": "2026-08-07"}))
    assert out["day_summary"] == [
        {"date": "2026-08-04", "day": "Tuesday 04 August", "free": 2},
        {"date": "2026-08-05", "day": "Wednesday 05 August", "free": 1}]
    one = _run(portal_data.execute(s, "check_availability",
                                   {"date_from": "2026-08-04", "date_to": "2026-08-04"}))
    assert "day_summary" not in one


# ── diary flavour (live e2e 2026-09-03: the kennels portal) ─────────────────
# A stay tenant's portal offered day/am/pm windows against an arrival→departure
# diary, so a half-day daycare visit was unbookable there while the email agent
# booked it to the minute. The portal now derives its diary tools from the
# tenant's own flavour, riding the email agent's executor.
def _stay_session(**kw):
    return _session(diary=("stay",
                           [{"label": "Pet name(s) & breed", "required": True,
                             "hint": "", "doc": False}],
                           [], ["Vaccination record"]), **kw)


def test_stay_tenant_gets_stay_shaped_tools_and_no_move():
    tools = {t["name"]: t for t in portal_data.tools_for(True, _stay_session())}
    assert "arrival" in tools["check_availability"]["input_schema"]["properties"]
    assert "arrival" in tools["create_booking"]["input_schema"]["properties"]
    assert "move_booking" not in tools          # window-shaped move can't move a stay
    # windows tenants keep the original catalogue untouched
    wt = {t["name"]: t for t in portal_data.tools_for(True, _session(
        diary=("windows", [], [], [])))}
    assert "booking_date" in wt["create_booking"]["input_schema"]["properties"]
    assert "move_booking" in wt


def test_service_tenant_gets_service_shaped_tools():
    s = _session(diary=("service", [], [{"name": "Cut & blow dry"}], []))
    tools = {t["name"]: t for t in portal_data.tools_for(True, s)}
    assert "service" in tools["check_availability"]["input_schema"]["properties"]
    assert "service" in tools["create_booking"]["input_schema"]["properties"]
    assert "move_booking" in tools


def test_stay_check_and_create_ride_the_email_executor(monkeypatch):
    calls = []
    state = {"bookings": []}

    def fake_execute(name, args):
        calls.append((name, args))
        if name == "check_availability":
            return {"available": True, "nights": 0}
        row = {"id": "b9", "booking_ref": "BK-2001", "booking_date": "2026-09-09",
               "status": "proposed", "summary": args["summary"]}
        state["bookings"].append(row)
        return {"held": True, "booking_ref": "BK-2001",
                "arrival": "2026-09-09T08:00", "departure": "2026-09-09T13:00"}

    monkeypatch.setattr(portal_data.scheduling, "make_executor",
                        lambda *a, **k: (fake_execute, state))
    statuses = {}
    monkeypatch.setattr(portal_data.db, "set_booking_status",
                        lambda bid, status, tid, **k: statuses.setdefault(bid, status))

    async def _no_push(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_push)
    s = _stay_session(contact={"id": "c1", "name": "Kris", "email": "k@x.com"},
                      booking_allowed=lambda: True)
    out = _run(portal_data.execute(s, "check_availability",
                                   {"arrival": "2026-09-09T08:00",
                                    "departure": "2026-09-09T13:00"}))
    assert out["available"] is True
    out = _run(portal_data.execute(s, "create_booking",
                                   {"arrival": "2026-09-09T08:00",
                                    "departure": "2026-09-09T13:00",
                                    "summary": "Daycare half day",
                                    "details": {"Pet name(s) & breed": "Bella"}}))
    assert out["booked"] is True and out["booking"]["booking_ref"] == "BK-2001"
    assert statuses["b9"] == "confirmed"        # self-serve confirm, same as windows
    # documents can't attach in chat — asked for by email instead of gating
    assert "Vaccination record" in out["documents_by_email"]
    held_args = calls[-1][1]
    assert held_args["details"]["customer_name"] == "Kris"
    assert held_args["details"]["Pet name(s) & breed"] == "Bella"
    assert held_args["summary"].endswith("— Kris")


def test_stay_missing_info_passes_through_for_the_chat_to_gather(monkeypatch):
    monkeypatch.setattr(
        portal_data.scheduling, "make_executor",
        lambda *a, **k: (lambda n, a2: {"error": "missing_info",
                                        "missing": ["Pet name(s) & breed"]},
                         {"bookings": []}))
    s = _stay_session(contact={"id": "c1", "email": "k@x.com"},
                      booking_allowed=lambda: True)
    out = _run(portal_data.execute(s, "create_booking",
                                   {"arrival": "2026-09-09T08:00",
                                    "departure": "2026-09-09T13:00", "summary": "x"}))
    assert out["error"] == "missing_info"


# ── cross-session create guards (live e2e 2026-09-03: BK-1020/BK-1021) ──────
# Two separate chats booked the SAME daycare day twice — the per-turn
# executor's overlap gate can't see across sessions. _booking_block refuses
# with the existing ref (already_booked) and enforces the optional per-customer
# cap (booking_limit); any guard failure ALLOWS the booking.
def _held_row(d, sb=30, lb=40, status="confirmed", ref="BK-1"):
    return {"id": "b1", "booking_ref": ref, "booking_date": d, "window": "day",
            "status": status, "start_block": sb, "length_blocks": lb}


def test_already_booked_refuses_an_overlapping_stay_span(monkeypatch):
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [_held_row("2099-09-25")])
    s = _stay_session(contact={"id": "c1", "email": "k@x.com"})
    out = portal_data._booking_block(
        s, {"arrival": "2099-09-25T09:00", "departure": "2099-09-25T15:00"}, "stay")
    assert out["error"] == "already_booked" and out["booking_ref"] == "BK-1"
    # a different day sails through
    assert portal_data._booking_block(
        s, {"arrival": "2099-09-26T09:00", "departure": "2099-09-26T15:00"},
        "stay") is None
    # past bookings never block a new one
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [_held_row("2020-01-01")])
    assert portal_data._booking_block(
        s, {"arrival": "2099-09-25T09:00", "departure": "2099-09-25T15:00"},
        "stay") is None


def test_already_booked_refuses_same_window_on_slot_diaries(monkeypatch):
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [
                            {"id": "b2", "booking_ref": "BK-2", "window": "10:00",
                             "booking_date": "2099-09-25", "status": "confirmed"}])
    s = _session(contact={"id": "c1", "email": "k@x.com"})
    out = portal_data._booking_block(
        s, {"booking_date": "2099-09-25", "window": "10:00"}, "windows")
    assert out["error"] == "already_booked"
    assert portal_data._booking_block(
        s, {"booking_date": "2099-09-25", "window": "14:00"}, "windows") is None
    # a whole-day booking blocks any window that date (and vice versa)
    out2 = portal_data._booking_block(
        s, {"booking_date": "2099-09-25", "window": "day"}, "windows")
    assert out2["error"] == "already_booked"


def test_booking_limit_caps_upcoming_and_zero_means_off(monkeypatch):
    rows = [_held_row("2099-09-25", ref="BK-1"), _held_row("2099-09-26", ref="BK-2")]
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: rows)
    capped = _stay_session(contact={"id": "c1", "email": "k@x.com"}, max_bookings=2)
    out = portal_data._booking_block(
        capped, {"arrival": "2099-10-01T09:00", "departure": "2099-10-01T15:00"},
        "stay")
    assert out["error"] == "booking_limit" and "at most 2" in out["message"]
    uncapped = _stay_session(contact={"id": "c1", "email": "k@x.com"})
    assert portal_data._booking_block(
        uncapped, {"arrival": "2099-10-01T09:00",
                   "departure": "2099-10-01T15:00"}, "stay") is None


def test_guard_failure_allows_the_booking(monkeypatch):
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: (_ for _ in ()).throw(RuntimeError("db")))
    s = _stay_session(contact={"id": "c1", "email": "k@x.com"})
    assert portal_data._booking_block(
        s, {"arrival": "2099-09-25T09:00", "departure": "2099-09-25T15:00"},
        "stay") is None
    assert portal._TOOL_SOFT_LABELS["already_booked"] == "Already in the diary"
    assert portal._TOOL_SOFT_LABELS["booking_limit"] == "Booking limit reached"


def test_stay_move_is_refused_toward_escalation():
    s = _stay_session(contact={"id": "c1", "email": "k@x.com"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": "BK-1", "new_date": "2026-09-10",
                                    "new_window": "am"}))
    assert out["error"] == "unsupported"
    assert "escalate_to_business" in out["message"]


def test_flavour_lookup_failure_falls_open_to_windows():
    # conftest blocks live Supabase, so the unmocked config lookup raises —
    # the portal degrades to the windows toolset instead of erroring out.
    assert portal_data._flavour(_session())[0] == "windows"


def test_prompt_forbids_promising_confirmation_emails():
    # Live e2e 2026-09-03 (Julius: "I don't remember ever building
    # Confirmation emails lol") — the assistant told a customer "You'll get a
    # confirmation email shortly" before the feature existed. With the toggle
    # OFF the prompt forbids the promise; with it ON (the built feature,
    # default) the assistant may honestly say one is coming.
    from app import portal_chat
    p = portal_chat.build_portal_prompt(business_name="W", scheduling_on=True)
    assert "this chat IS the confirmation" in p
    assert "NEVER promise a confirmation email" in p
    on = portal_chat.build_portal_prompt(business_name="W", scheduling_on=True,
                                         booking_confirmation=True)
    assert "confirmation email with the booking reference is also sent" in on
    assert "NEVER promise a confirmation email" not in on


def test_portal_booking_sends_the_confirmation_email(monkeypatch):
    """The transactional confirmation (Julius 2026-09-03): template-built, ref
    + footer included, gated on session.confirmation_on, and best-effort — a
    mail failure never costs the booking."""
    sent = []

    async def fake_send(tid, to, subject, body, **kw):
        sent.append({"to": to, "subject": subject, "body": body})
    import app.mailbox as mailbox
    monkeypatch.setattr(mailbox, "send_mail", fake_send)
    monkeypatch.setattr(portal, "email_footer",
                        lambda tid, cfg, email: "\n\n---\nYour reference: WILL-7K**")
    monkeypatch.setattr(portal_data.db, "set_booking_status", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.crm, "note_contact_activity", lambda *a, **k: None)

    async def _no_push(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_push)
    row = {"id": "b1", "booking_ref": "BK-3001", "booking_date": "2026-09-25",
           "status": "proposed"}
    s = _session(contact={"id": "c1", "name": "Kris Deane", "email": "k@x.com"})
    out = _run(portal_data._finalise_portal_booking(
        s, row, "Daycare full day for Bella and Coco", "Kris Deane", "k@x.com",
        "2026-09-25T07:30 → 2026-09-25T17:30"))
    assert out["booked"] is True
    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == "k@x.com" and "BK-3001" in mail["subject"]
    assert "Hi Kris," in mail["body"]
    assert "Friday 25 September, 7:30am" in mail["body"]      # pretty times
    assert "Your reference: WILL-7K**" in mail["body"]         # footer rode along
    # Toggle OFF → no send; a failing send never breaks the booking.
    s2 = _session(contact={"id": "c1", "name": "K", "email": "k@x.com"},
                  confirmation_on=False)
    out2 = _run(portal_data._finalise_portal_booking(
        s2, row, "x", "K", "k@x.com", "2026-09-25"))
    assert out2["booked"] is True and len(sent) == 1

    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(mailbox, "send_mail", boom)
    out3 = _run(portal_data._finalise_portal_booking(
        s, row, "x", "K", "k@x.com", "2026-09-25"))
    assert out3["booked"] is True                              # best-effort


def test_stay_prompt_speaks_stays_and_drops_move():
    from app import portal_chat
    p = portal_chat.build_portal_prompt(business_name="Willowbrook Kennels",
                                        scheduling_on=True, diary_flavour="stay")
    assert "books STAYS" in p and "same-day" in p.lower()
    assert "move_booking" not in p              # stays reschedule via escalation
    svc = portal_chat.build_portal_prompt(business_name="Shear Bliss",
                                          scheduling_on=True,
                                          diary_flavour="service")
    assert "set SERVICES" in svc and "move_booking" in svc


def test_on_result_corrects_the_pill_after_a_refused_action(monkeypatch):
    # Honest pills: the settled label consults the RESULT — a refused
    # create_booking must never read "Booked you in".
    from types import SimpleNamespace
    from app import portal_chat

    def usage(n=1):
        return SimpleNamespace(input_tokens=n, output_tokens=n,
                               cache_read_input_tokens=0,
                               cache_creation_input_tokens=0)

    responses = [
        SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", id="tu-1", name="create_booking",
            input={"summary": "x"})],
            stop_reason="tool_use", usage=usage(), model="m"),
        SimpleNamespace(content=[SimpleNamespace(
            type="text", text='{"reply": "Sorry, that did not book."}')],
            stop_reason="end_turn", usage=usage(), model="m"),
    ]

    class FakeClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)
            self.n = 0

        async def _create(self, **kw):
            self.n += 1
            return responses[self.n - 1]

    monkeypatch.setattr(portal_chat, "_client", FakeClient())
    seen = []

    async def executor(name, args):
        return {"error": "invalid_window", "message": "no"}

    async def on_result(name, out):
        seen.append((name, out.get("error")))

    reply, totals, model = _run(portal_chat.answer_portal(
        "book it", system="s", tools=[{"name": "create_booking",
                                       "input_schema": {"type": "object"}}],
        tool_executor=executor, on_result=on_result, tenant_id="t1"))
    assert seen == [("create_booking", "invalid_window")]
    assert reply == "Sorry, that did not book."
    assert portal._TOOL_FAIL_LABELS["create_booking"] == "Couldn't book that"


def test_settled_label_soft_for_coaching_hard_for_refusals():
    # Live e2e 2026-09-03 (Julius): the intake gate's missing_info was shown
    # as "Couldn't book that" beside a same-turn "Booked you in" — the
    # customer's booking succeeded first try from THEIR side. Coaching errors
    # settle neutral; terminal refusals keep the honest failure label; a
    # success keeps its call-time label.
    lbl = portal._settled_label
    assert lbl("create_booking", {"error": "missing_info"}) == "Needed a few more details"
    assert lbl("create_booking", {"error": "not_identified"}) == "Needed to check who's asking"
    assert lbl("create_booking", {"error": "invalid_window"}) == "Couldn't book that"
    assert lbl("create_booking", {"error": "slot_full"}) == "Couldn't book that"
    assert lbl("move_booking", {"error": "unsupported"}) == "Couldn't move that booking"
    assert lbl("create_booking", {"booked": True}) is None
    assert lbl("check_availability", {"error": "bad_dates"}) is None   # read-tool label stays


# ── the on-file customer record (Julius 2026-09-03) ─────────────────────────
# The portal asked a verified customer their dogs' breeds with
# dog_names_breeds sitting in the CRM: the identified customer's defined
# custom fields now reach the model — prompt-side when pre-verified,
# on_file in the identity tools' results when verified mid-turn.
def test_profile_lines_render_defined_fields_only(monkeypatch):
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [
                            {"entity": "contact", "key": "dog_names_breeds",
                             "label": "Dog names & breeds"},
                            {"entity": "company", "key": "dog_names_breeds",
                             "label": "WRONG entity"}])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    s = _session(contact={"id": "c1", "email": "k@x.com", "phone": "07700 1",
                          "custom_data": {"dog_names_breeds": "Bella and Coco, cockers",
                                          "undefined_key": "never shown"}})
    lines = portal_data._profile_lines(s)
    assert lines == ["Phone: 07700 1",
                     "Dog names & breeds: Bella and Coco, cockers"]
    # cached per identity — a switch mid-chat can't reuse the old person's data
    s.contact = {"id": "c2", "email": "other@x.com", "custom_data": {}}
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [])
    assert portal_data._profile_lines(s) == []


def test_profile_lines_carry_open_promises_both_ways(monkeypatch):
    # Julius 2026-09-03: "is there anything you still need from me?" answers
    # from the ledger — the record carries open promises, both directions.
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [])
    monkeypatch.setattr(portal_data.db, "crm_open_promises",
                        lambda tid, contact_id=None, **k: [
                            {"direction": "we_owe", "due_hint": "after the stay",
                             "description": "Send the invoice"},
                            {"direction": "they_owe", "due_hint": "",
                             "description": "Confirm the booster date"}])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    s = _session(contact={"id": "c1", "email": "k@x.com", "custom_data": {}})
    lines = portal_data._profile_lines(s)
    assert "The business still owes them: Send the invoice (after the stay)" in lines
    assert "Still needed from this customer: Confirm the booster date" in lines


def test_contact_view_carries_on_file(monkeypatch):
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [{"entity": "contact", "key": "d",
                                           "label": "Dogs"}])
    s = _session(contact={"id": "c1", "name": "Kris", "email": "k@x.com",
                          "reference_code": "WILL-7K3M",
                          "custom_data": {"d": "two cockers"}})
    view = portal_data._contact_view(s)
    assert view["on_file"] == ["Dogs: two cockers"]
    assert view["reference_masked"] == "WILL-7K**"      # code still masked


def test_profile_lookup_failure_is_empty_not_fatal():
    # conftest blocks live Supabase → the definitions read raises → [] on file
    s = _session(contact={"id": "c1", "email": "k@x.com",
                          "custom_data": {"d": "x"}})
    assert portal_data._profile_lines(s) == []
    assert "on_file" not in portal_data._contact_view(s)


def test_prompt_carries_the_customer_record():
    from app import portal_chat
    p = portal_chat.build_portal_prompt(
        business_name="Willowbrook", scheduling_on=True, diary_flavour="stay",
        verified_name="Kris",
        customer_profile=["Dog names & breeds: Bella and Coco, cockers"])
    assert "<customer_record>" in p
    assert "Bella and Coco, cockers" in p
    assert "Never re-ask for anything listed above" in p
    # anonymous sessions get no record section
    anon = portal_chat.build_portal_prompt(business_name="W", scheduling_on=True)
    assert "<customer_record>" not in anon


def test_diary_tools_absent_without_scheduling():
    names = {t["name"] for t in portal_data.tools_for(False)}
    assert not names & {"check_availability", "create_booking", "move_booking"}
    s = _session(scheduling_on=False)
    out = _run(portal_data.execute(s, "create_booking",
                                   {"booking_date": "2026-08-01", "window": "am",
                                    "summary": "x"}))
    assert out["error"] == "no_diary"


def test_escalation_lands_as_needs_attention_inbox_row(monkeypatch):
    rows = []
    def _insert(tid, from_address, subject, body_in, draft, **kw):
        rows.append({"tenant": tid, "from": from_address, "subject": subject,
                     "body": body_in, **kw})
        return "log-1", True
    monkeypatch.setattr(portal_data.db, "insert_email_log", _insert)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    out = _run(portal_data.execute(s, "escalate_to_business",
                                   {"message": "Do you fit heat pumps?"}))
    assert out["escalated"] is True
    assert rows[0]["from"] == "s@x.com"
    assert rows[0]["status"] == "pending"
    assert rows[0]["thread_state"] == "needs_attention"
    assert rows[0]["category"] == "needs_reply"
    assert "heat pumps" in rows[0]["body"]


def test_executor_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference", _boom)
    out = _run(portal_data.execute(_session(), "lookup_reference", {"code": "X-1"}))
    assert out["error"] == "lookup_failed"
    assert _run(portal_data.execute(_session(), "nope", {}))["error"] == "unknown_tool"


# ── lookup endpoint ──────────────────────────────────────────────────────────
def test_lookup_endpoint_scopes_by_slug(client, monkeypatch):
    _page_env(monkeypatch)
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda tid, code: {"id": "c1", "name": "Sarah",
                                           "email": "s@x.com",
                                           "reference_code": "ACME-7K3M"}
                        if code == "ACME-7K3M" else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    ok = client.post("/api/portal/lookup",
                     json={"slug": "acme-plumbing", "ref": "ACME-7K3M"}).json()
    assert ok["found"] is True
    assert ok["customer"]["reference_masked"] == "ACME-7K**"
    assert "ACME-7K3M" not in str(ok)            # never echoed back
    # unknown business slug → not found, no data
    miss = client.post("/api/portal/lookup",
                       json={"slug": "someone-else", "ref": "ACME-7K3M"}).json()
    assert miss == {"found": False}


def test_lookup_by_email_returns_no_data(client, monkeypatch):
    """Identity hotfix: name+email on the lookup endpoint unlocks nothing —
    no contact created, no bookings returned, just 'verify via the chat'."""
    _page_env(monkeypatch)
    created = []
    monkeypatch.setattr(portal.db, "crm_get_or_create_contact",
                        lambda *a, **k: created.append(a) or {})
    out = client.post("/api/portal/lookup",
                      json={"slug": "acme-plumbing", "name": "S",
                            "email": "known@x.com"}).json()
    assert out == {"found": False, "verification_required": True}
    assert not created


# ── chat endpoint (engine faked) ─────────────────────────────────────────────
def test_chat_streams_done_event_with_customer(client, monkeypatch):
    _page_env(monkeypatch)
    async def _fake_answer(question, history, **kw):
        return "Hello!", {"input_tokens": 1, "output_tokens": 1}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    monkeypatch.setattr(portal.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: {"id": "c1", "name": "Sarah",
                                           "email": "s@x.com",
                                           "reference_code": "ACME-7K3M"})
    monkeypatch.setattr(portal.db, "crm_get_contact",
                        lambda cid, tid, **k: {"id": "c1", "name": "Sarah",
                                               "email": "s@x.com",
                                               "reference_code": "ACME-7K3M"})
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing", "question": "hi",
                            "ref": "ACME-7K3M"})
    assert res.status_code == 200
    import json
    events = [json.loads(l) for l in res.text.strip().splitlines()]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["reply"] == "Hello!"
    # masked code + opaque session token — the full code never rides the wire
    assert done["customer"]["reference_masked"] == "ACME-7K**"
    assert "reference_code" not in done["customer"]
    token = done["customer"]["session"]
    assert token and "ACME-7K3M" not in token
    # the token resolves back to the contact on a later turn (same process)
    res2 = client.post("/api/portal/chat",
                       json={"slug": "acme-plumbing", "question": "hi again",
                             "session": token})
    done2 = [json.loads(l) for l in res2.text.strip().splitlines()][-1]
    assert done2["customer"]["reference_masked"] == "ACME-7K**"
    # …but not for another business
    assert portal._resolve_session("other-tenant", token) is None


def test_typed_code_verifies_without_the_model(client, monkeypatch):
    """The identity switch (Julius, live e2e 2026-09-01): a bare reference
    code is settled deterministically — the model must not run at all."""
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    ran = []

    async def _fake_answer(*a, **kw):
        ran.append(1)
        return "should not run", {}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: ({"id": "c1", "name": "Sarah Vale",
                                            "email": "s@x.com",
                                            "reference_code": "ACME-7K3M"}
                                           if code == "ACME-7K3M" else None))
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing", "question": " acme-7k3m. "})
    events = [_json.loads(l) for l in res.text.strip().splitlines()]
    done = [e for e in events if e["type"] == "done"][0]
    assert ran == []                                # ZERO tokens
    assert "Thanks, Sarah" in done["reply"] and "verified" in done["reply"]
    assert done["customer"]["reference_masked"] == "ACME-7K**"
    assert done["customer"]["session"]              # sticky identity minted
    assert done["trail"][0]["label"] == "Looked up your reference"


def test_verified_code_resumes_the_blocked_question(client, monkeypatch):
    """Verifying should ANSWER the question the gate blocked, not make the
    customer ask twice (Julius, live e2e 2026-09-01): code typed after a
    gated ask → the model runs on the ORIGINAL question, identified."""
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    asked = []

    async def _fake_answer(question, history, **kw):
        asked.append(question)
        return "You owe £75.00 on INV-1008.", {"input_tokens": 1,
                                               "output_tokens": 1}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    monkeypatch.setattr(portal.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: {"id": "c1", "name": "Sarah",
                                           "email": "s@x.com",
                                           "reference_code": "ACME-7K3M"})
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    res = client.post("/api/portal/chat", json={
        "slug": "acme-plumbing", "question": "ACME-7K3M",
        "history": [
            {"role": "user", "content": "Can I see my invoices — do I owe anything?"},
            {"role": "assistant", "content": "I just need to identify you first…"},
        ]})
    events = [_json.loads(l) for l in res.text.strip().splitlines()]
    done = [e for e in events if e["type"] == "done"][0]
    # The model ran exactly once, on the question the gate blocked.
    assert asked == ["Can I see my invoices — do I owe anything?"]
    assert done["reply"] == "You owe £75.00 on INV-1008."
    assert done["customer"]["session"]              # identity stuck
    # The identity lookup leads the trail, before the answer's own pills.
    assert done["trail"][0]["label"] == "Looked up your reference"
    tool_events = [e for e in events if e["type"] == "tool"]
    assert tool_events[0]["label"] == "Looked up your reference"


def test_wrong_code_bounces_without_the_model(client, monkeypatch):
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    ran = []

    async def _fake_answer(*a, **kw):
        ran.append(1)
        return "should not run", {}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda t, c: None)
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing", "question": "ACME-XXXX"})
    done = [_json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert ran == [] and "doesn't match" in done["reply"]
    assert done["customer"] is None


def test_gated_ask_gets_the_identity_question_for_free(client, monkeypatch):
    """'My invoices' from an unverified visitor used to run the model (which
    searched the KB before even asking who they were). The invite gate
    answers it for free."""
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    ran = []

    async def _fake_answer(*a, **kw):
        ran.append(1)
        return "should not run", {}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing",
                            "question": "Can I see my invoices — do I owe anything?"})
    done = [_json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert ran == []
    assert "This page is for Acme Plumbing's customers" in done["reply"]
    assert "ACME-XXXX" in done["reply"]             # the tenant's code shape
    assert done["customer"] is None and done["trail"] == []


def test_open_questions_from_strangers_meet_the_invite_gate(client, monkeypatch):
    # Invite-only (Kris 2026-09-24): a prospect asking about prices no longer
    # reaches the model — they're told the page is for existing customers.
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    ran = []

    async def _fake_answer(question, history, **kw):
        ran.append(1)
        return "A boiler service is £80.", {}, "model-x"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing",
                            "question": "How much is a boiler service?"})
    done = [_json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert ran == []
    assert "This page is for Acme Plumbing's customers" in done["reply"]


def test_typed_email_answers_the_same_whether_or_not_on_file(client, monkeypatch):
    """A stranger's email and a customer's get one reply — the gate never
    says who is a customer here — but only the customer is sent a code."""
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    known = {"id": "c1", "name": "Sam", "email": "sam@x.com",
             "reference_code": "ACME-7K3M"}
    monkeypatch.setattr(portal_data.db, "crm_contact_by_email",
                        lambda tid, email, **k: dict(known) if email == "sam@x.com" else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    created = []
    monkeypatch.setattr(portal_data.db, "crm_get_or_create_contact",
                        lambda *a, **k: created.append(a) or {})
    monkeypatch.setattr(portal, "ensure_reference",
                        lambda tid, email, name=None: dict(known))
    sent = []

    async def _send(tid, name, slug, contact):
        sent.append(contact["email"]); return True
    monkeypatch.setattr(portal, "send_verification_email", _send)

    def _ask(q):
        res = client.post("/api/portal/chat",
                          json={"slug": "acme-plumbing", "question": q})
        return [_json.loads(l) for l in res.text.strip().splitlines()][-1]
    stranger = _ask("it's new@person.com")
    customer = _ask("sam@x.com")
    assert stranger["reply"] == customer["reply"]
    assert "on its way" in customer["reply"]
    assert sent == ["sam@x.com"] and created == []
    assert stranger["customer"] is None and customer["customer"] is None


def test_verified_question_waiting_is_answered_after_the_code(client, monkeypatch):
    """The ask the gate held back runs once the code verifies — the customer
    shouldn't have to ask twice."""
    import json as _json
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    _page_env(monkeypatch)
    contact = {"id": "c1", "name": "Sam", "email": "sam@x.com",
               "reference_code": "ACME-7K3M"}
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda tid, code: dict(contact) if code == "ACME-7K3M" else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(portal.usage, "record_anthropic", lambda *a, **k: None)
    asked = []

    async def _fake_answer(question, history, **kw):
        asked.append(question)
        return "A boiler service is £80.", {"input_tokens": 1, "output_tokens": 1}, "m"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake_answer)
    history = [{"role": "user", "content": "How much is a boiler service?"},
               {"role": "assistant", "content": "This page is for Acme Plumbing's customers."}]
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing", "question": "ACME-7K3M",
                            "history": history})
    done = [_json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert asked == ["How much is a boiler service?"]
    assert done["reply"] == "A boiler service is £80."


def test_chat_for_unknown_slug_degrades_politely(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.post("/api/portal/chat",
                      json={"slug": "ghost", "question": "hi"})
    assert res.status_code == 200
    assert "isn't available" in res.text


# ── identity verification (hotfix 2026-07-26) ────────────────────────────────
def test_mask_code():
    assert portal_data.mask_code("TEST-KRH7") == "TEST-KR**"
    assert portal_data.mask_code("ACME-7K3M") == "ACME-7K**"
    assert portal_data.mask_code("") == ""
    assert portal_data.mask_code(None) == ""


_EXISTING = {"id": "c1", "name": "Velox", "email": "v@x.com",
             "reference_code": "TEST-KRH7"}


def _verify_session(monkeypatch, existing=_EXISTING, bookings=(), sent=True,
                    allowed=True):
    monkeypatch.setattr(portal_data.db, "crm_contact_by_email",
                        lambda tid, email, **k: dict(existing) if existing else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda *a, **k: list(bookings))
    emails = []
    async def _send(contact):
        emails.append(contact)
        return sent
    return _session(contact=None,
                    ensure_reference=lambda tid, email, name=None: dict(existing or
                        {"id": "new1", "name": name, "email": email,
                         "reference_code": "TEST-ABCD"}),
                    verify_allowed=lambda email: allowed,
                    send_verification=_send), emails


def test_find_contact_on_existing_account_emails_the_code_and_never_links(monkeypatch):
    s, emails = _verify_session(monkeypatch)
    out = _run(portal_data.execute(s, "find_contact",
                                   {"name": "Velox Velcee", "email": "v@x.com"}))
    assert out["verification_sent"] is True
    assert s.contact is None                       # NOT linked by name+email
    assert emails and emails[0]["reference_code"] == "TEST-KRH7"
    import json as _json
    assert "TEST-KRH7" not in _json.dumps(out)     # code never enters the chat


def test_find_contact_with_bookings_but_no_contact_row_still_verifies(monkeypatch):
    s, emails = _verify_session(
        monkeypatch, existing=None,
        bookings=[{"id": "b1", "booking_date": "2099-01-01"}])
    # ensure_reference creates the row + mints the code for the email path
    s.ensure_reference = lambda tid, email, name=None: {
        "id": "c9", "email": email, "reference_code": "TEST-ZZZZ"}
    out = _run(portal_data.execute(s, "find_contact",
                                   {"name": "Velox", "email": "v@x.com"}))
    assert out["verification_sent"] is True
    assert s.contact is None


def test_find_contact_never_creates_a_customer(monkeypatch):
    """Invite-only (Kris 2026-09-24): an email with no contact and no
    bookings is refused — no contact created, nobody linked."""
    s, emails = _verify_session(monkeypatch, existing=None, bookings=())
    created = []
    monkeypatch.setattr(portal_data.db, "crm_get_or_create_contact",
                        lambda *a, **k: created.append(a) or {})
    s.ensure_reference = lambda *a, **k: created.append(a) or {}
    out = _run(portal_data.execute(s, "find_contact",
                                   {"name": "Newbie", "email": "n@x.com"}))
    assert out["error"] == "not_invited"
    assert s.contact is None and created == [] and emails == []


def test_find_contact_verification_rate_limit_and_send_failure(monkeypatch):
    s, _ = _verify_session(monkeypatch, allowed=False)
    out = _run(portal_data.execute(s, "find_contact",
                                   {"name": "V", "email": "v@x.com"}))
    assert out["error"] == "verification_limit" and s.contact is None
    s2, _ = _verify_session(monkeypatch, sent=False)
    out2 = _run(portal_data.execute(s2, "find_contact",
                                    {"name": "V", "email": "v@x.com"}))
    assert out2["error"] == "verification_unavailable" and s2.contact is None


def test_verify_allowed_limits_per_email_and_ip():
    portal._VERIFY_EMAIL_BUCKETS.clear()
    portal._VERIFY_IP_BUCKETS.clear()
    # 3 per email per hour
    assert [portal._verify_allowed("a@x.com", "1.1.1.1") for _ in range(4)] == \
        [True, True, True, False]
    # 5 per IP per hour across addresses (3 already burned on this IP)
    assert portal._verify_allowed("b@x.com", "1.1.1.1") is True
    assert portal._verify_allowed("c@x.com", "1.1.1.1") is True
    assert portal._verify_allowed("d@x.com", "1.1.1.1") is False
    # a fresh IP is unaffected
    assert portal._verify_allowed("d@x.com", "2.2.2.2") is True


def test_session_tokens_roundtrip_and_scope(monkeypatch):
    monkeypatch.setattr(portal.db, "crm_get_contact",
                        lambda cid, tid, **k: {"id": cid, "email": "v@x.com"})
    tok = portal._mint_session("t1", "c1")
    assert portal._resolve_session("t1", tok)["id"] == "c1"
    assert portal._resolve_session("t2", tok) is None      # other business
    assert portal._resolve_session("t1", "garbage") is None


def test_verification_email_contains_code_and_is_audited(monkeypatch):
    sent, events = [], []
    async def _send_mail(tid, to, subject, body, **k):
        sent.append({"to": to, "subject": subject, "body": body})
    import app.mailbox as mailbox
    monkeypatch.setattr(mailbox, "send_mail", _send_mail)
    monkeypatch.setattr(portal.db, "log_agent_event",
                        lambda *a, **k: events.append(a))
    ok = _run(portal.send_verification_email(
        "t1", "Test Blam8", "test-blam8",
        {"name": "Velox Velcee", "email": "v@x.com", "reference_code": "TEST-KRH7"}))
    assert ok is True
    assert sent[0]["to"] == "v@x.com"
    assert "TEST-KRH7" in sent[0]["body"]
    # link carries the ref so the click lands personalised + pre-verified
    assert "/c/test-blam8?ref=TEST-KRH7" in sent[0]["body"]
    assert "ignore this email" in sent[0]["body"]
    assert events                                   # audit-logged
    # no code on the contact → refuses rather than sending an empty mail
    assert _run(portal.send_verification_email(
        "t1", "B", "s", {"email": "v@x.com"})) is False


# ── booking budget window ────────────────────────────────────────────────────
def test_booking_budget_is_five_per_day_per_visitor():
    portal._BOOK_BUCKETS.clear()
    allowed = [portal._booking_allowed("t1", "1.2.3.4") for _ in range(6)]
    assert allowed == [True] * 5 + [False]
    # another visitor (or business) has their own budget
    assert portal._booking_allowed("t1", "5.6.7.8") is True
    assert portal._booking_allowed("t2", "1.2.3.4") is True


# ── settings ─────────────────────────────────────────────────────────────────
def _settings_env(monkeypatch):
    saved = {}
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"name": "Acme", "portal_slug": "acme"})
    return saved


def test_portal_settings_save(client, monkeypatch):
    saved = _settings_env(monkeypatch)
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_enabled": "on",
                            "portal_description": "Boilers",
                            "portal_slug": "acme"},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert res.status_code == 303 and "portal=saved" in res.headers["location"]
    assert saved["portal_enabled"] is True
    assert saved["portal_show_prices"] is False      # unticked checkbox
    assert saved["portal_description"] == "Boilers"


def test_portal_settings_rejects_bad_and_taken_slugs(client, monkeypatch):
    _settings_env(monkeypatch)
    bad = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "Bad Slug!"},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "slug_invalid" in bad.headers["location"]
    monkeypatch.setattr(dashboard.db, "set_portal_slug", lambda *a, **k: False)
    taken = client.post("/app/settings/portal",
                        data={"csrf_token": "x", "portal_slug": "new-name"},
                        cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "slug_taken" in taken.headers["location"]


def test_portal_settings_save_reschedule_mode(client, monkeypatch):
    saved = _settings_env(monkeypatch)
    client.post("/app/settings/portal",
                data={"csrf_token": "x", "portal_slug": "acme",
                      "portal_reschedule_mode": "approve"},
                cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert saved["portal_reschedule_mode"] == "approve"
    # anything else (absent, tampered) normalises to the default
    client.post("/app/settings/portal",
                data={"csrf_token": "x", "portal_slug": "acme",
                      "portal_reschedule_mode": "yolo"},
                cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert saved["portal_reschedule_mode"] == "auto"


# ── reschedule modes + CRM timeline (2026-07-31) ─────────────────────────────
# uuid-shaped: a non-uuid booking_id never reaches db.get_booking any more.
_B_ID = "d14ae542-ffa2-459d-b257-820437ad233c"


def _movable_env(monkeypatch):
    monkeypatch.setattr(portal_data.db, "get_booking",
                        lambda bid, tid, **k: {"id": bid, "contact": "s@x.com",
                                               "status": "confirmed",
                                               "booking_ref": "BK-1001",
                                               "booking_date": "2026-08-05",
                                               "window": "am",
                                               "summary": "Boiler service"})


def test_move_booking_accepts_the_booking_ref(monkeypatch):
    """Models mangle long uuids when re-typing them — the human-sized ref must
    work as the key, resolved against the caller's OWN bookings only."""
    real = {"id": "d14ae542-ffa2-459d-b257-820437ad233c", "contact": "s@x.com",
            "status": "confirmed", "booking_ref": "BK-1001",
            "booking_date": "2026-08-05", "window": "am",
            "summary": "Boiler service"}

    def _get_booking(bid, tid, **k):
        # Mirror prod: a non-uuid hitting the uuid column RAISES (22P02) —
        # the executor must never pass a ref-shaped id here.
        if not portal_data._UUID_RE.match(bid):
            raise RuntimeError("invalid input syntax for type uuid")
        return dict(real) if bid == real["id"] else None
    monkeypatch.setattr(portal_data.db, "get_booking", _get_booking)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [dict(real)])
    moved = []
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda tid, bid, day, window: (
                            moved.append(bid) or
                            ({**real, "booking_date": day, "window": window}, None)))
    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _no_cal)
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda *a, **k: True)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": "bk-1001",   # ref, wrong case
                                    "new_date": "2026-08-07", "new_window": "pm"}))
    assert out["moved"] is True and moved == [real["id"]]
    # a mangled uuid still refuses cleanly (no crash) — the ref is the
    # recovery route, which is why the tool description now prefers it
    moved.clear()
    out3 = _run(portal_data.execute(
        s, "move_booking",
        {"booking_id": "d14ae542-ffa2-459d-b199-820437ad233c",
         "new_date": "2026-08-07", "new_window": "pm"}))
    assert out3["error"] == "not_found" and not moved
    # a ref that isn't theirs still refuses
    out2 = _run(portal_data.execute(s, "move_booking",
                                    {"booking_id": "BK-9999", "new_date": "2026-08-07",
                                     "new_window": "pm"}))
    assert out2["error"] == "not_found"


# ── party-aware portal (docs/modules/module-job-parties.md Phase 3) ──────────────────
# The portal was already email-keyed, so payers/approvers see their documents
# the moment Phase 1 routes them. These pin what Phase 3 ADDS: the on-site
# party sees and may move the visit (and ONLY on_site grants that), and a
# party without the money duty gets the don't-discuss-their-money guard.
_PARTY_B = {"id": _B_ID, "contact": "agent@lettings.example",
            "status": "confirmed", "booking_ref": "BK-1077",
            "booking_date": "2099-09-10", "window": "am",
            "summary": "Rewire kitchen"}


def test_landing_greets_the_onsite_party_with_their_visit(client, monkeypatch):
    """The ?ref= landing and the chat must agree: an on-site party's next
    visit greets them on arrival, not "no upcoming bookings"."""
    _page_env(monkeypatch)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, ref: {"id": "c2", "name": "Priya",
                                          "email": "priya@x.co",
                                          "reference_code": "ACME-7K3M"})
    monkeypatch.setattr(portal.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(portal.db, "job_party_memberships",
                        lambda tid, email, client=None: [
                            {"booking_id": _B_ID, "on_site": True,
                             "label": "tenant", "source": "confirmed"}])
    monkeypatch.setattr(portal.db, "bookings_by_ids",
                        lambda tid, ids, client=None: [dict(_PARTY_B)])
    res = client.get("/c/acme-plumbing?ref=acme-7k3m")
    assert "Hi Priya" in res.text
    assert "Rewire kitchen" in res.text and "BK-1077" in res.text


def test_onsite_party_sees_the_visit_they_attend(monkeypatch):
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "job_party_memberships",
                        lambda tid, email, client=None: [
                            {"booking_id": _B_ID, "on_site": True,
                             "label": "tenant", "source": "confirmed"}])
    monkeypatch.setattr(portal_data.db, "bookings_by_ids",
                        lambda tid, ids, client=None: [dict(_PARTY_B)])
    s = _session(contact={"id": "c2", "name": "Priya", "email": "priya@x.co"})
    out = _run(portal_data.execute(s, "list_my_bookings", {}))
    assert out["upcoming_bookings"][0]["booking_ref"] == "BK-1077"
    assert out["upcoming_bookings"][0]["your_role"] == "tenant"


def test_onsite_party_moves_the_visit_but_other_duties_cannot(monkeypatch):
    monkeypatch.setattr(portal_data.db, "get_booking",
                        lambda bid, tid, **k: dict(_PARTY_B))
    moved = []
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda tid, bid, day, window: moved.append(bid) or
                        ({**_PARTY_B, "booking_date": day, "window": window},
                         None))

    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _no_cal)
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda *a, **k: True)
    members = [{"booking_id": _B_ID, "on_site": True, "label": "tenant",
                "source": "confirmed"}]
    monkeypatch.setattr(portal_data.db, "job_party_memberships",
                        lambda tid, email, client=None: list(members))
    s = _session(contact={"id": "c2", "name": "Priya", "email": "priya@x.co"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID,
                                    "new_date": "2099-09-12",
                                    "new_window": "pm"}))
    assert out["moved"] is True and moved == [_B_ID]
    # The landlord who PAYS (and is kept informed) still may not move an
    # appointment at someone else's property — only on_site grants the diary.
    members[0] = {"booking_id": _B_ID, "on_site": False, "informed": True,
                  "pays": True, "source": "confirmed"}
    s2 = _session(contact={"id": "c3", "email": "landlord@x.co"})
    out2 = _run(portal_data.execute(s2, "move_booking",
                                    {"booking_id": _B_ID,
                                     "new_date": "2099-09-12",
                                     "new_window": "pm"}))
    assert out2["error"] == "not_found" and moved == [_B_ID]


def test_onsite_party_can_move_by_booking_ref_too(monkeypatch):
    """The ref-recovery path searches their on-site jobs as well as their own
    bookings — a tenant types BK-1077, not a uuid."""
    monkeypatch.setattr(portal_data.db, "get_booking",
                        lambda bid, tid, **k:
                        dict(_PARTY_B) if bid == _B_ID else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "job_party_memberships",
                        lambda tid, email, client=None: [
                            {"booking_id": _B_ID, "on_site": True,
                             "source": "owner"}])
    monkeypatch.setattr(portal_data.db, "bookings_by_ids",
                        lambda tid, ids, client=None: [dict(_PARTY_B)])
    moved = []
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda tid, bid, day, window: moved.append(bid) or
                        ({**_PARTY_B, "booking_date": day, "window": window},
                         None))

    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _no_cal)
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda *a, **k: True)
    s = _session(contact={"id": "c2", "email": "priya@x.co"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": "bk-1077",
                                    "new_date": "2099-09-12",
                                    "new_window": "pm"}))
    assert out["moved"] is True and moved == [_B_ID]


def test_party_without_the_money_duty_gets_the_guard_note(monkeypatch):
    monkeypatch.setattr(portal_data.db, "invoices_for_contact",
                        lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "quotes_for_contact",
                        lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "job_party_memberships",
                        lambda tid, email, client=None: [
                            {"booking_id": _B_ID, "on_site": True,
                             "pays": False, "approves": False,
                             "source": "confirmed"}])
    s = _session(contact={"id": "c2", "email": "priya@x.co"})
    inv = _run(portal_data.execute(s, "list_my_invoices", {}))
    assert inv["count"] == 0 and "Never discuss" in inv["party_note"]
    assert "payment_note" not in inv       # no payment routes for non-payers
    q = _run(portal_data.execute(s, "list_my_quotes", {}))
    assert q["count"] == 0 and "party_note" in q
    # A non-party stranger keeps today's plain empty answer — no note.
    import app.payments as payments
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda t: False)
    monkeypatch.setattr(portal_data.db, "get_invoice_settings",
                        lambda *a, **k: {})
    monkeypatch.setattr(portal_data.db, "job_party_memberships",
                        lambda tid, email, client=None: [])
    s3 = _session(contact={"id": "c4", "email": "stranger@x.co"})
    inv3 = _run(portal_data.execute(s3, "list_my_invoices", {}))
    assert "party_note" not in inv3 and "payment_note" in inv3


def test_move_booking_respects_the_reschedule_budget(monkeypatch):
    _movable_env(monkeypatch)
    moved = []
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda *a, **k: moved.append(a) or ({}, None))
    s = _session(contact={"id": "c1", "email": "s@x.com"},
                 move_allowed=lambda: False)
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID, "new_date": "2026-08-07",
                                    "new_window": "pm"}))
    assert out["error"] == "limit_reached" and not moved


def test_move_budget_is_five_per_day_per_visitor():
    portal._MOVE_BUCKETS.clear()
    allowed = [portal._move_allowed("t1", "1.2.3.4") for _ in range(6)]
    assert allowed == [True] * 5 + [False]
    assert portal._move_allowed("t1", "5.6.7.8") is True


def test_approve_mode_requests_instead_of_moving(monkeypatch):
    _movable_env(monkeypatch)
    rows, moved, crm_rows = [], [], []
    def _insert(tid, from_address, subject, body_in, draft, **kw):
        rows.append({"from": from_address, "subject": subject,
                     "body": body_in, **kw})
        return "log-9", True
    monkeypatch.setattr(portal_data.db, "insert_email_log", _insert)
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda *a, **k: moved.append(a) or ({}, None))
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda tid, contact, atype, **k:
                        crm_rows.append((atype, k)) or True)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"},
                 reschedule_mode="approve", move_allowed=lambda: True)
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID, "new_date": "2026-08-07",
                                    "new_window": "pm"}))
    assert out["requested"] is True and "moved" not in out
    assert not moved                          # the diary was never touched
    assert rows[0]["from"] == "s@x.com"
    assert rows[0]["thread_state"] == "needs_attention"
    assert rows[0]["category"] == "needs_reply"
    assert "BK-1001" in rows[0]["body"] and "2026-08-07 (pm)" in rows[0]["body"]
    assert s.move_requests == ["log-9"]
    assert crm_rows[0][0] == "reschedule_request"
    assert "2026-08-05 (am)" in crm_rows[0][1]["summary"]
    # the customer is told it's a request, not a done deal
    assert "will confirm" in out["message"]


def test_auto_move_writes_the_crm_timeline(monkeypatch):
    _movable_env(monkeypatch)
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda tid, bid, day, window: (
                            {"id": bid, "booking_ref": "BK-1001",
                             "booking_date": day, "window": window,
                             "status": "confirmed", "summary": "Boiler service"},
                            None))
    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _no_cal)
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    crm_rows = []
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda tid, contact, atype, **k:
                        crm_rows.append((atype, k)) or True)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID, "new_date": "2026-08-07",
                                    "new_window": "pm"}))
    assert out["moved"] is True
    assert crm_rows[0][0] == "calendar_event"
    assert "Rescheduled via the portal" in crm_rows[0][1]["summary"]
    # each move keys its own feed row — moving twice never dedupes away
    assert crm_rows[0][1]["source_id"] == f"{_B_ID}:2026-08-07:pm"
    assert s.moves_made == [_B_ID]


def test_create_booking_writes_the_crm_timeline(monkeypatch):
    monkeypatch.setattr(
        portal_data.scheduling, "place_hold",
        lambda tid, day, window, summary, **kw: (
            {"id": "b1", "booking_ref": "BK-1001", "booking_date": str(day),
             "window": window, "status": "proposed", "summary": summary}, None))
    monkeypatch.setattr(portal_data.db, "set_booking_status", lambda *a, **k: None)
    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    crm_rows = []
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda tid, contact, atype, **k:
                        crm_rows.append((atype, k)) or True)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"},
                 booking_allowed=lambda: True)
    out = _run(portal_data.execute(s, "create_booking",
                                   {"booking_date": "2026-08-01", "window": "am",
                                    "summary": "Boiler service"}))
    assert out["booked"] is True
    assert crm_rows[0][0] == "calendar_event"
    assert crm_rows[0][1]["source_id"] == "b1"
    assert "Booked via the portal" in crm_rows[0][1]["summary"]


def test_escalation_ingests_into_the_crm(monkeypatch):
    monkeypatch.setattr(portal_data.db, "insert_email_log",
                        lambda *a, **kw: ("log-1", True))
    monkeypatch.setattr(portal_data.db, "get_email_log_entry",
                        lambda log_id, tid: {"id": log_id,
                                             "from_address": "s@x.com"})
    ingested = []
    monkeypatch.setattr(portal_data.crm, "ingest_email_log_row",
                        lambda tid, row: ingested.append(row) or {})
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    out = _run(portal_data.execute(s, "escalate_to_business",
                                   {"message": "Do you fit heat pumps?"}))
    assert out["escalated"] is True
    assert ingested and ingested[0]["id"] == "log-1"


def test_chat_activity_logs_questions_and_masks_the_code(monkeypatch):
    logged = []
    monkeypatch.setattr(portal.crm, "note_contact_activity",
                        lambda tid, contact, atype, **k:
                        logged.append((atype, k)) or True)
    s = _session(contact={"id": "c1", "email": "s@x.com",
                          "reference_code": "ACME-7K3M"})
    portal._note_chat_activity(s, "My code is ACME-7K3M — do I owe anything?",
                               ["lookup_reference", "list_my_invoices"])
    assert logged and logged[0][0] == "portal_chat"
    assert "ACME-7K3M" not in logged[0][1]["summary"]
    assert "ACME-7K**" in logged[0][1]["summary"]
    # identity-only turns stay out of the timeline
    logged.clear()
    portal._note_chat_activity(s, "ACME-7K3M", ["lookup_reference"])
    assert not logged
    # anonymous turns never log
    portal._note_chat_activity(_session(), "opening hours?",
                               ["search_knowledge_base"])
    assert not logged
    # a turn whose action wrote its own feed row doesn't double up
    s.bookings_made.append("b1")
    portal._note_chat_activity(s, "what about Thursday?",
                               ["check_availability"])
    assert not logged


def test_prompt_frames_approve_mode_as_a_request():
    import app.portal_chat as portal_chat
    auto = portal_chat.build_portal_prompt(business_name="Acme",
                                           scheduling_on=True)
    appr = portal_chat.build_portal_prompt(business_name="Acme",
                                           scheduling_on=True,
                                           reschedule_approve=True)
    assert "confirms reschedules PERSONALLY" in appr
    assert "NEVER say the new time is booked" in appr
    assert "confirms reschedules PERSONALLY" not in auto


def test_prompt_carries_the_verified_visitor(monkeypatch):
    # Ref-identity friction (Kris 2026-08-31, twice live): the ?ref link
    # verified the visitor server-side, the greeting named them and their
    # booking — and the model still asked for their email before acting.
    # Rule 0 now tells the model what the request handler already knows.
    import app.portal_chat as portal_chat
    p = portal_chat.build_portal_prompt(business_name="Acme", scheduling_on=True,
                                        verified_name="Kris Deane")
    assert "IDENTIFIED VISITOR" in p and "Kris Deane" in p
    assert "never ask them to identify themselves" in p
    assert "trust the tool" in p                     # tools stay the authority
    anon = portal_chat.build_portal_prompt(business_name="Acme", scheduling_on=True)
    assert "IDENTIFIED VISITOR" not in anon          # anonymous flow unchanged


def test_confirmation_when_is_words_for_every_diary_shape():
    """Live 2026-09-03: the portal confirmation email for a service booking read
    '2026-09-07 (09:00)' — the raw ISO date a customer must never see. Every
    diary shape prettifies; unknown shapes pass through untouched."""
    from app import portal_data
    assert portal_data._pretty_when("2026-09-07 (09:00)") == "Monday 7 September, 9:00am"
    assert portal_data._pretty_when("2026-09-21T07:30 → 2026-09-21T17:30") == (
        "Monday 21 September, 7:30am → Monday 21 September, 5:30pm")
    assert portal_data._pretty_when("Monday 7 September") == "Monday 7 September"


def test_confirmation_when_speaks_arrival_windows():
    """An arrival-window portal booking confirms 'Monday 7 September, in the
    morning' — never a clock time the customer was not given."""
    from app import portal_data
    assert portal_data._pretty_when("2026-09-07 (am)") == "Monday 7 September, in the morning"
    assert portal_data._pretty_when("2026-09-07 (pm)") == "Monday 7 September, in the afternoon"
    assert portal_data._pretty_when("2026-09-07 (day)") == "Monday 7 September, during the day"


def test_portal_create_booking_names_the_exact_detail_keys():
    """The portal model only ever learned the labels from a refusal — every first
    create_booking call missed (live 2026-09-03/04). The keys ride the tool."""
    gate = [{"label": "Full name", "required": True, "hint": ""},
            {"label": "Nature of the work", "required": True, "hint": ""}]
    s = _session(diary=("service", gate, [{"name": "Cut & blow dry"}], []))
    tools = {t["name"]: t for t in portal_data.tools_for(True, s)}
    desc = tools["create_booking"]["input_schema"]["properties"]["details"]["description"]
    assert "'Full name', 'Nature of the work'" in desc
    # the email agent's shared constant is untouched
    assert "'Nature of the work'" not in next(
        t for t in scheduling.SERVICE_TOOLS if t["name"] == "hold_booking"
    )["input_schema"]["properties"]["details"]["description"]


def test_profile_lines_carry_facts_from_earlier_bookings(monkeypatch):
    """Kris 2026-09-04: the inscription given by EMAIL lived on BK-1005's details,
    so the portal asked for it again. Facts from the customer's live bookings ride
    the record — newest first, one line per label, plumbing keys and labels already
    on the record skipped, blanks dropped, capped."""
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [{"entity": "contact", "key": "site_address",
                                           "label": "Site address"}])
    monkeypatch.setattr(portal_data.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [
        {"booking_ref": "BK-1005", "booking_date": "2026-09-08", "summary": "Consultation",
         "details": {"Service": "Design consultation", "portal": True, "customer_name": "Kris",
                     "Full name": "Helen Deane", "Phone number": "07700 900000",
                     "Email address": "kris@x.com",
                     "Inscription wording & lettering style": "MARGARET ROSE HUGHES — serif",
                     "Site address": "St Mary's (older value)", "Type of visit": "Consultation",
                     "Nature of the work": "New slate headstone", "Access notes": ""}},
        {"booking_ref": "BK-1006", "booking_date": "2026-09-07", "summary": "Survey",
         "details": {"Service": "Site survey",
                     "Nature of the work": "Additional inscription (older, superseded)",
                     "Memorial or loved one's name": "Margaret Hughes"}},
    ])
    s = _session(contact={"id": "c1", "email": "k@x.com",
                          "custom_data": {"site_address": "St Mary's churchyard, Nunney"}})
    lines = portal_data._profile_lines(s)
    assert lines[0] == "Site address: St Mary's churchyard, Nunney"
    facts = [l for l in lines if l.startswith("From their booking")]
    assert facts == [
        "From their booking BK-1005 (Design consultation, 8th Sep 26): "
        "Inscription wording & lettering style: MARGARET ROSE HUGHES — serif",
        "From their booking BK-1005 (Design consultation, 8th Sep 26): "
        "Nature of the work: New slate headstone",
        "From their booking BK-1006 (Site survey, 7th Sep 26): "
        "Memorial or loved one's name: Margaret Hughes",
    ]
    # plumbing keys never surface; the record's own label wins over the booking copy;
    # identity details (a sister's name on BK-1008, live 2026-09-04) never travel
    assert not any("customer_name" in l or "portal" in l.lower().split(":")[0] for l in facts)
    assert not any("Helen" in l or "07700 900000" in l or "Email address" in l for l in facts)
    assert not any("older value" in l for l in lines)
    # a bookings failure never costs the CRM lines
    def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", boom)
    s2 = _session(contact={"id": "c9", "email": "z@x.com",
                           "custom_data": {"site_address": "Somewhere"}})
    assert portal_data._profile_lines(s2) == ["Site address: Somewhere"]


def test_the_cached_record_rebuilds_when_the_contact_changes(monkeypatch):
    """Run #8: the owner moved the customer's level on the CRM mid-session and
    the portal kept answering from the record it had cached at sign-in. A
    contact write bumps the record's version; the profile re-reads and rebuilds."""
    from app import record_versions
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, **k: [{"entity": "contact", "key": "level", "label": "Level"}])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    fresh = {"id": "c1", "email": "k@x.com", "custom_data": {"level": "Prompted"}}
    reads = []
    monkeypatch.setattr(portal_data.db, "crm_get_record",
                        lambda entity, rid, tid, client=None: reads.append(rid) or dict(fresh))
    s = _session(contact={"id": "c1", "email": "k@x.com", "custom_data": {"level": "Helped"}})
    assert portal_data._profile_lines(s) == ["Level: Helped"]
    assert portal_data._profile_lines(s) == ["Level: Helped"] and reads == []   # cached, no re-read
    record_versions.bump("t1", "c1")                                            # what a CRM write does
    assert portal_data._profile_lines(s) == ["Level: Prompted"] and reads == ["c1"]
    assert s.contact["custom_data"]["level"] == "Prompted"                    # the session's contact too
    assert portal_data._profile_lines(s) == ["Level: Prompted"] and reads == ["c1"]   # cached again
    # aged out: rebuilt even with no bump
    s.profile = s.profile[:4] + (s.profile[4] - portal_data.PROFILE_TTL_S - 1,)
    assert portal_data._profile_lines(s) == ["Level: Prompted"] and reads == ["c1", "c1"]
    # a failed re-read keeps the record it has, never raises
    monkeypatch.setattr(portal_data.db, "crm_get_record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    record_versions.bump("t1", "c1")
    assert portal_data._profile_lines(s) == ["Level: Prompted"]


def test_contact_writers_bump_the_record_version():
    import inspect
    from app import db as _db, record_versions
    assert "record_versions.bump(tenant_id, contact_id)" in inspect.getsource(_db.crm_update_contact)
    assert "record_versions.bump(tenant_id, record_id)" in inspect.getsource(_db.crm_merge_custom_data)
    v = record_versions.get("tx", "cx")
    assert record_versions.bump("tx", "cx") == v + 1 and record_versions.get("tx", "cx") == v + 1


def test_a_contact_ladder_reads_as_a_level_line(monkeypatch):
    """Run #8: "DVSA driver's record: Prompted" became "your record has been
    prompted" in the chat. A ladder renders as the dependant path does."""
    ladder = {"entity": "contact", "key": "dvsa", "label": "DVSA driver's record",
              "field_type": "dropdown",
              "options": ["Introduced", "Helped", "Prompted", "Independent", "Reflection", "Test ready"],
              "ladder": {"notify": "approve", "gates": []}}
    plain = {"entity": "contact", "key": "centre", "label": "Test centre",
             "field_type": "dropdown", "options": ["Hereford", "Worcester"]}
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions", lambda tid, **k: [ladder, plain])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    s = _session(contact={"id": "c1", "email": "k@x.com",
                          "custom_data": {"dvsa": "Prompted", "centre": "Hereford"}})
    assert portal_data._profile_lines(s) == [
        "Level on the DVSA driver's record: Prompted — 3 of 6, next Independent",
        "Test centre: Hereford"]
    assert portal_data._ladder_line(ladder, "Test ready") == \
        "Level on the DVSA driver's record: Test ready — 6 of 6, the top level"
    assert portal_data._ladder_line(ladder, "not a level") is None


def test_the_verified_session_stays_identified_for_its_own_contact(monkeypatch):
    """Run #8: a customer verified in this session, then gave their email —
    and the identity switch sent a code to someone already in. The session
    that verified the contact stays identified for that email. (Invite-only:
    sessions no longer CREATE contacts, so verifying is the only way in.)"""
    sam = {"id": "c-sam", "name": "Sam", "email": "sam@x.com", "reference_code": "HART-1AB2"}
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda tid, code: dict(sam) if code == "HART-1AB2" else None)
    monkeypatch.setattr(portal_data.db, "crm_contact_by_email",
                        lambda tid, email: dict(sam) if email == "sam@x.com" else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact",
                        lambda tid, email, **k: [{"id": "b1"}])
    monkeypatch.setattr(portal_data, "_upcoming", lambda s: [])
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions", lambda tid, **k: [])
    sent = []

    async def send(contact):
        sent.append(contact["email"]); return True
    s = _session(send_verification=send)
    first = portal_data._lookup_reference(s, {"code": "HART-1AB2"})
    assert first["found"] is True and "sam@x.com" in s.own_emails
    again = _run(portal_data._find_contact(s, {"name": "Sam", "email": "sam@x.com"}))
    assert again["found"] is True and "verification_sent" not in again and sent == []
    assert again["message"].startswith("Already identified")
    # a different email in the same session is a returning customer: the code path
    monkeypatch.setattr(portal_data.db, "crm_contact_by_email",
                        lambda tid, email: {"id": "c-x", "email": email, "reference_code": "HART-9ZZ9"})
    other = _run(portal_data._find_contact(s, {"name": "Pat", "email": "pat@x.com"}))
    assert other.get("verification_sent") is True and sent == ["pat@x.com"]

from tests.booking_fixtures import carried_config


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())


def test_a_portal_move_reads_as_a_date_a_person_reads(monkeypatch):
    """Live 2026-09-30 (Hartwell rig): the owner's Today card for a portal move read
    "Moved · Now 2026-10-09 (am)". The push, the agent log and the CRM timeline now
    say "9th Oct 26 in the morning" (app/dates.pretty_date + scheduling.window_words)."""
    _movable_env(monkeypatch)
    monkeypatch.setattr(portal_data.scheduling, "move_booking",
                        lambda tid, bid, day, window: (
                            {"id": bid, "booking_ref": "BK-1001", "booking_date": day, "window": window,
                             "status": "confirmed", "summary": "Boiler service"}, None))
    async def _no_cal(*a, **k):
        return None
    import app.calendar_sync as calendar_sync
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _no_cal)
    monkeypatch.setattr(calendar_sync, "push_bookings", _no_cal)
    pushes, logs, crm_rows = [], [], []
    monkeypatch.setattr(portal_data.push, "notify", lambda *a, **k: pushes.append(k))
    monkeypatch.setattr(portal_data.db, "log_agent_event", lambda tid, kind, text, **k: logs.append(text))
    monkeypatch.setattr(portal_data.crm, "note_contact_activity",
                        lambda tid, contact, atype, **k: crm_rows.append(k) or True)
    s = _session(contact={"id": "c1", "name": "Sarah", "email": "s@x.com"})
    out = _run(portal_data.execute(s, "move_booking",
                                   {"booking_id": _B_ID, "new_date": "2026-10-09", "new_window": "am"}))
    assert out["moved"] is True
    assert pushes[0]["body"] == "Now 9th Oct 26 in the morning"
    assert "9th Oct 26 in the morning" in logs[0] and "2026-10-09" not in logs[0]
    assert "→ 9th Oct 26 in the morning" in crm_rows[0]["summary"]
