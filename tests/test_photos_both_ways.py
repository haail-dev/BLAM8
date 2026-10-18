"""Photos both ways (Kris 2026-09-26: "We just don't have anywhere to store,
display or attach photos… That would be a helpful feature for people that need
evidence of whatever both ways").

  * a reply can carry files — ticked from the customer's record or taken there
    and then — through Gmail and Outlook alike, and they show on our bubble;
  * a phone photo is made fit to email (an iPhone HEIC or a 6 MB original
    becomes a JPEG);
  * the job page keeps photos of the job; a freelancer's job sheet sends
    photos and a note back to the office.

Run: python -m pytest tests/test_photos_both_ways.py -q
"""
import asyncio
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import app.dashboard as dashboard
from app import documents, gmail, mailbox, photos
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

from tests.test_job_sheets import put, rig, run  # noqa: F401 — the rig fixture


def _png(w=40, h=30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _noisy_png(w=1400, h=1400) -> bytes:
    """A PNG over 1.5 MB (noise doesn't compress) — a camera original's stand-in."""
    import os
    im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_a_gmail_reply_carries_its_attachments():
    msg = gmail.build_reply_mime("a@x.co", "Re: Leak", "Photo attached.", "<m1@x>", "",
                                 attachments=[("boiler.jpg", b"\xff\xd8jpeg", "image/jpeg")])
    parts = list(msg.iter_attachments())
    assert [p.get_filename() for p in parts] == ["boiler.jpg"]
    assert parts[0].get_content_type() == "image/jpeg"
    assert msg["In-Reply-To"] == "<m1@x>"


def test_an_outlook_reply_carries_them_too(monkeypatch):
    posted = []

    async def fake_access(tid, box):
        return "tok", "microsoft", {}

    async def fake_post(access, path, json=None):
        posted.append((path, json))
    monkeypatch.setattr(mailbox, "_access", fake_access)
    monkeypatch.setattr(mailbox, "_send_as", lambda conn: None)
    monkeypatch.setattr(mailbox.graph, "graph_post", fake_post)
    asyncio.run(mailbox.send_reply("t1", "a@x.co", "Leak", "Photo attached.", source_message_id="m1",
                                   attachments=[("boiler.jpg", b"jpeg", "image/jpeg")]))
    path, body = posted[0]
    assert path == "/me/messages/m1/reply"
    att = body["message"]["attachments"][0]
    assert att["name"] == "boiler.jpg" and att["contentBytes"] == "anBlZw=="


def test_a_camera_photo_is_made_fit_to_email():
    big = _noisy_png()
    assert len(big) > 1_500_000
    name, data, ct = photos.web_ready(big, "IMG_0042.png", "image/png")
    assert name == "IMG_0042.jpg" and ct == "image/jpeg" and len(data) < len(big)
    small = _png()
    assert photos.web_ready(small, "fault.png", "image/png") == ("fault.png", small, "image/png")
    assert photos.web_ready(b"%PDF", "cp12.pdf", "application/pdf") == ("cp12.pdf", b"%PDF", "application/pdf")


def test_reply_files_come_off_this_customers_record_only(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", client=None)
    docs = {"d1": {"id": "d1", "filename": "cp12.pdf", "content_type": "application/pdf",
                   "contact_email": "sarah@x.co", "url_or_path": "t1/sarah/cp12.pdf"},
            "d2": {"id": "d2", "filename": "other.pdf", "content_type": "application/pdf",
                   "contact_email": "someone@else.co", "url_or_path": "t1/else/other.pdf"}}
    stored = []
    monkeypatch.setattr(dashboard.db, "get_document", lambda i, t, client=None: docs.get(i))
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(documents, "download", lambda p, client=None: b"%PDF-bytes")

    def fake_store(tid, **k):
        stored.append(k)
        return {"id": "new1", "filename": k["filename"], "url_or_path": "t1/sarah/new.png",
                "content_type": k["content_type"], "size_bytes": len(k["data"])}
    monkeypatch.setattr(documents, "store", fake_store)
    entry = {"from_address": "Sarah@x.co"}
    files, rows = dashboard._reply_files(ctx, entry, ["d1"], [("after.png", _png(), "image/png")])
    assert [f[0] for f in files] == ["cp12.pdf", "after.png"]
    assert stored[0]["contact_email"] == "sarah@x.co" and stored[0]["contact_id"] == "c1"
    assert [r["id"] for r in rows] == ["d1", "new1"]
    with pytest.raises(HTTPException):
        dashboard._reply_files(ctx, entry, ["d2"], [])


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_send_route_attaches_and_our_bubble_remembers(client, monkeypatch):
    from tests.test_send_is_instant import _wire
    _wire(monkeypatch, [], [])
    sent, inserted = [], []

    async def fake_send(tid, to, subject, body, **k):
        sent.append(k.get("attachments"))
    monkeypatch.setattr(dashboard.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(dashboard.db, "insert_document", lambda tid, **k: inserted.append(k))
    monkeypatch.setattr(dashboard, "_reply_files", lambda ctx, e, ids, new: (
        [("cp12.pdf", b"%PDF", "application/pdf")],
        [{"id": "d1", "filename": "cp12.pdf", "url_or_path": "t1/sarah/cp12.pdf",
          "content_type": "application/pdf", "size_bytes": 4}]) if ids == ["d1"] else ([], []))
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Here's the certificate.", "attach_doc": "d1",
                          "return_to": "/app/conversations"})
    assert r.status_code == 303, r.text
    assert sent == [[("cp12.pdf", b"%PDF", "application/pdf")]]
    assert inserted[0]["source"] == "sent" and inserted[0]["email_log_id"] == "log-1"
    assert inserted[0]["storage_path"] == "t1/sarah/cp12.pdf"


def test_the_freelancer_sends_photos_and_a_note_back(rig, monkeypatch):  # noqa: F811
    from app import job_sheets
    put(rig, "p-jake")
    run(job_sheets.reconcile("t1", "b1"))
    token = rig["sheets"][0]["token"]
    kept = []
    monkeypatch.setattr(documents, "store", lambda tid, **k: kept.append(k) or {"id": "x"})
    c = TestClient(app)
    r = c.post(f"/j/{token}/photos", files=[("photo", ("fault.png", _png(), "image/png"))],
               data={"note": "Diverter valve is seized — needs a new one"}, follow_redirects=False)
    assert r.status_code == 303 and "sent_back" in r.headers["location"]
    assert kept[0]["booking_id"] == "b1" and kept[0]["contact_email"] == "sarah@example.com"
    assert kept[0]["label"] == "From Jake"
    p = rig["pushes"][-1]
    assert p["kind"] == "team_photos" and "1 photo and a note" in p["title"]
    assert "Diverter valve" in p["body"]
    r = c.post(f"/j/{token}/photos", data={"note": ""}, follow_redirects=False)
    assert "nothing" in r.headers["location"]


def test_the_job_page_keeps_photos_of_the_job(client, monkeypatch):
    kept = []
    monkeypatch.setattr(dashboard.db, "get_booking", lambda bid, tid, client=None:
                        {"id": "b1", "contact": "Sarah@x.co", "summary": "Boiler service"})
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(documents, "store", lambda tid, **k: kept.append(k) or {"id": "x"})
    r = client.post("/app/diary/b1/photos", follow_redirects=False,
                    files=[("photo", ("before.png", _png(), "image/png")),
                           ("photo", ("after.png", _png(), "image/png"))])
    assert r.status_code == 303 and "msg=photos" in r.headers["location"]
    assert [k["booking_id"] for k in kept] == ["b1", "b1"]
    assert kept[0]["contact_email"] == "sarah@x.co" and kept[0]["contact_id"] == "c1"


def test_a_reply_that_cant_go_says_why_on_the_thread(client, monkeypatch):
    """Live 2026-09-26: the store failed and the phone showed a bare
    {"detail": …} page. Nothing is sent; the thread says why."""
    from tests.test_send_is_instant import _wire
    _wire(monkeypatch, [], [])

    def boom(*a, **k):
        raise HTTPException(status_code=503, detail="Couldn't keep IMG_3103.png — try again.")
    monkeypatch.setattr(dashboard, "_reply_files", boom)
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Hi", "attach_doc": "d1",
                          "return_to": "/app/conversations/view?contact=sam%40x.co"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "send_err=Couldn%27t+keep+IMG_3103.png" in loc and "sent=1" not in loc


def test_stored_files_are_read_and_written_with_the_service_role():
    """Live 2026-09-26: the user's client can't write the private bucket, and
    download() called db.db() on a function."""
    import inspect
    src = inspect.getsource(documents.download)
    assert "db.db()" not in src
    dash = inspect.getsource(dashboard._reply_files) + inspect.getsource(dashboard.diary_job_photos)
    assert "documents.store(" in dash and "client=ctx.client)\n        if not row" not in dash
    assert "booking_id=booking_id, client=ctx.client" not in dash
