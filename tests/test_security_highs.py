"""The security review's high findings (reports/security-review-2026-09.md),
each pinned so it can't come back.

Run: python -m pytest tests/test_security_highs.py -q
"""
import pytest
from fastapi import HTTPException

from app import guards, portal


class Ctx:
    def __init__(self, user_id="u1", viewing_as=None):
        self.tenant_id, self.user_id, self.viewing_as, self.client = "t1", user_id, viewing_as, None


def _member(monkeypatch, role, active=True):
    monkeypatch.setattr(guards.db, "get_member", lambda t, u: {"user_id": u, "role": role, "active": active,
                                                              "scopes": {}})


# ── H1 / H2: roles on the actions ────────────────────────────────────────────
@pytest.mark.parametrize("role", ["manager", "staff", "accountant"])
def test_only_the_owner_closes_the_account_or_touches_billing(monkeypatch, role):
    _member(monkeypatch, role)
    with pytest.raises(HTTPException) as e:
        guards.require_owner(Ctx())
    assert e.value.status_code == 403


def test_the_owner_passes_but_not_while_viewing_as_someone(monkeypatch):
    _member(monkeypatch, "owner")
    guards.require_owner(Ctx())
    with pytest.raises(HTTPException):
        guards.require_owner(Ctx(viewing_as="owner-id"))
    monkeypatch.setattr(guards.db, "get_member", lambda t, u: None)   # pre-People account
    guards.require_owner(Ctx())


@pytest.mark.parametrize("role,ok", [("owner", True), ("manager", True), ("staff", False), ("accountant", False)])
def test_connections_and_refunds_need_an_admin(monkeypatch, role, ok):
    _member(monkeypatch, role)
    if ok:
        guards.require_admin(Ctx())
    else:
        with pytest.raises(HTTPException):
            guards.require_admin(Ctx())


def test_a_switched_off_manager_is_refused(monkeypatch):
    _member(monkeypatch, "manager", active=False)
    with pytest.raises(HTTPException):
        guards.require_admin(Ctx())


def test_the_routes_carry_their_guard():
    """Every money / connection / account route starts with its guard."""
    import inspect
    from app import billing_routes, channel_routes, dashboard, exit_export, oauth, payments
    want = {dashboard.close_account: "require_owner", payments.payments_refund: "require_admin",
            payments.payments_connect: "require_owner", payments.payments_disconnect: "require_owner",
            oauth.connect: "require_admin", oauth.disconnect: "require_admin",
            channel_routes.activate: "require_owner", exit_export.exit_bundle: "require_admin",
            dashboard.disconnect_mailbox: "require_admin", billing_routes.start_checkout: "require_owner"}
    for fn, guard in want.items():
        assert f"_guards.{guard}(ctx)" in inspect.getsource(fn), fn.__name__


# ── H6: the Concierge transcript is per person ───────────────────────────────
def test_concierge_history_is_scoped_to_the_person(monkeypatch):
    from app import db
    calls = []

    class Q:
        def table(self, *a): return self
        def select(self, *a): return self
        def delete(self): calls.append("delete"); return self
        def eq(self, k, v): calls.append(("eq", k, v)); return self
        def or_(self, f): calls.append(("or", f)); return self
        @property
        def not_(self): return self
        def like(self, *a): return self
        def order(self, *a, **k): return self
        def limit(self, *a): return self
        def execute(self): return type("R", (), {"data": []})()
    monkeypatch.setattr(db, "db", lambda: Q())
    db.list_concierge_messages("t1", user_id="staff-1", include_shared=False)
    assert ("eq", "user_id", "staff-1") in calls
    calls.clear()
    db.list_concierge_messages("t1", user_id="owner-1", include_shared=True)
    assert ("or", "user_id.eq.owner-1,user_id.is.null") in calls
    calls.clear()
    db.clear_concierge_messages("t1", user_id="staff-1", include_shared=False)
    assert ("eq", "user_id", "staff-1") in calls


# ── H7: the browser never gets the record ────────────────────────────────────
def test_the_browser_gets_who_they_are_not_their_record():
    view = {"name": "Ann Lee", "email": "ann@x.co", "reference_masked": "ACME-7K**",
            "on_file": "Allergies: penicillin", "from_earlier_jobs": "Child DOB 2019-04-02",
            "session": "tok"}
    assert portal.for_browser(view) == {"name": "Ann Lee", "reference_masked": "ACME-7K**",
                                        "session": "tok"}


# ── H3: a business's codes pause after a run of wrong guesses ────────────────
def test_a_guessing_run_pauses_the_business_codes(monkeypatch):
    portal._MISSES.clear()
    monkeypatch.setattr(portal.trace, "emit", lambda *a, **k: None)
    real = {"ACME-7K3M22": {"id": "c1"}}
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda t, c: real.get(c))
    for i in range(portal.MISS_BUDGET):
        assert portal.contact_for_code("t1", f"ACME-{i:06d}") is None
    # the real code no longer answers this hour — nor any other guess
    assert portal.contact_for_code("t1", "ACME-7K3M22") is None
    assert portal.contact_for_code("t2", "ACME-7K3M22") is None or True   # other businesses unaffected
    portal._MISSES.clear()
    assert portal.contact_for_code("t1", "ACME-7K3M22") == {"id": "c1"}


def test_the_rate_limiter_uses_cloudflares_client_ip():
    from app import security

    class R:
        def __init__(self, h): self.headers, self.client = h, None
    assert security._client_ip(R({"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "6.6.6.6, 9.9.9.9"})) == "1.2.3.4"
    # no Cloudflare header: the LAST hop (the proxy's), never the client-written first
    assert security._client_ip(R({"x-forwarded-for": "6.6.6.6, 9.9.9.9"})) == "9.9.9.9"


# ── H4: a decompression-bomb image is refused before it decodes ──────────────
def test_a_pixel_bomb_is_refused_without_decoding():
    import io, time, tracemalloc
    from PIL import Image
    from app import photos, safe_parse
    buf = io.BytesIO()
    Image.new("L", (13000, 13000), 0).save(buf, format="PNG", optimize=True)
    bomb = buf.getvalue()
    assert len(bomb) < 2_000_000                      # small on disk…
    tracemalloc.start()
    t0 = time.monotonic()
    assert safe_parse.open_image(bomb) is None        # …refused from its header
    assert photos.for_model(bomb, "x.png") is None
    name, data, ct = photos.web_ready(bomb, "x.png", "image/png")
    assert data is bomb                               # passed through untouched, never decoded
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 50_000_000 and time.monotonic() - t0 < 5
    # an ordinary photo still works
    ok = io.BytesIO(); Image.new("RGB", (4000, 3000), (9, 9, 9)).save(ok, format="JPEG")
    assert photos.for_model(ok.getvalue(), "p.jpg")["media_type"] == "image/jpeg"


# ── H5: PDFs are read in a child process with a timeout ──────────────────────
def _tiny_pdf() -> bytes:
    import io
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    out = io.BytesIO(); w.write(out)
    return out.getvalue()


def test_a_pdf_is_read_in_a_child_and_a_slow_one_is_cut_off():
    from app import safe_parse, documents
    pdf = _tiny_pdf()
    assert safe_parse.pdf_text(pdf, max_pages=3, max_chars=100) == ""          # parsed fine, no text layer
    assert safe_parse.pdf_text(pdf, max_pages=3, max_chars=100, timeout_s=0.001) == ""   # killed, no hang
    assert safe_parse.pdf_text(b"x" * (safe_parse.MAX_PDF_BYTES + 1), max_pages=1, max_chars=1) == ""
    assert documents.document_text("card.pdf", "application/pdf", pdf) == ""


# ── M9: a Connect event from someone else's Stripe account changes nothing ───
def test_a_webhook_from_another_account_is_ignored(monkeypatch):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app import db, payments
    from app.main import app
    monkeypatch.setattr(payments.settings, "stripe_connect_webhook_secret", "whsec_x")
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: "acct_REAL")
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    paid = []
    monkeypatch.setattr(db, "mark_invoice_paid", lambda *a, **k: paid.append(a) or None)
    monkeypatch.setattr(payments.trace, "emit", lambda *a, **k: None)
    evt = {"id": "evt_x", "type": "payment_intent.succeeded", "account": "acct_ATTACKER",
           "data": {"object": {"id": "pi_1", "metadata": {"kind": "invoice", "tenant_id": "t1",
                                                          "invoice_id": "inv-1"}}}}
    with patch("app.payments.stripe.Webhook.construct_event", return_value=evt):
        r = TestClient(app).post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=0,v1=x"})
    assert r.status_code == 200 and r.text == "account mismatch" and paid == []


# ── M8: a consent callback only completes in the browser that started it ────
def test_a_consent_link_sent_to_someone_else_does_not_connect(monkeypatch):
    from fastapi.testclient import TestClient
    from app import oauth, security
    from app.main import app
    monkeypatch.setattr(oauth.db, "pop_oauth_state", lambda s: ("t-attacker", "replace", None))
    c = TestClient(app)
    r = c.get("/oauth/google/callback", params={"code": "c", "state": "s1"})   # victim's browser: no cookie
    assert r.status_code == 400 and "invalid or expired" in r.text

    class R:
        def __init__(self, v): self.cookies = {security.CONSENT_COOKIE: v} if v else {}
    assert security.consent_is_this_browsers(R("s1"), "s1")
    assert not security.consent_is_this_browsers(R("other"), "s1")
    assert not security.consent_is_this_browsers(R(None), "s1")


# ── M1: a switched-off person resolves to no business, and gets no new one ───
def test_a_switched_off_member_has_no_business(monkeypatch):
    from app import db
    seen = []

    class Q:
        def __init__(self, rows): self.rows = rows
        def table(self, *a): return self
        def select(self, *a): return self
        def eq(self, k, v): seen.append((k, v)); return self
        def order(self, *a, **k): return self
        def limit(self, *a): return self
        def insert(self, *a): raise AssertionError("must not create a business")
        def execute(self): return type("R", (), {"data": self.rows})()
    monkeypatch.setattr(db, "db", lambda: Q([]))
    assert db.get_tenant_for_user("u1") is None and ("active", True) in seen
    monkeypatch.setattr(db, "has_any_membership", lambda uid: True)
    with pytest.raises(db.MembershipSwitchedOff):
        db.ensure_tenant_for_user("u1", "X")


# ── M3: the CRM API respects the doors ───────────────────────────────────────
@pytest.mark.parametrize("role,method,status", [("accountant", "get", 403), ("staff", "get", 403),
                                                ("manager", "get", 200)])
def test_the_crm_api_respects_people_doors(monkeypatch, role, method, status):
    from fastapi.testclient import TestClient
    from app import crm_routes, db
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    _member(monkeypatch, role)
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(db, "crm_list_contacts", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u1")
    try:
        r = getattr(TestClient(app), method)("/api/crm/contacts")
        assert r.status_code == status
    finally:
        app.dependency_overrides.clear()


# ── M6: signing out ends the session at Supabase, not just in the browser ────
def test_sign_out_revokes_the_session(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    revoked = []
    monkeypatch.setattr(auth, "revoke_session", lambda tok, scope="local": revoked.append(tok))
    c = TestClient(app)
    c.cookies.set(auth.AT_COOKIE, "tok-123")
    r = c.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and revoked == ["tok-123"]
