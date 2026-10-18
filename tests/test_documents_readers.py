"""Documents phase 1 (Kris 2026-09-04): the mail readers hand over attachment
metadata, one fetch brings the bytes, the pipeline keeps them on the record.

Run: python -m pytest tests/test_documents_readers.py -q
"""
import asyncio
import base64

from app import documents, gmail, mailbox


PAYLOAD = {
    "mimeType": "multipart/mixed",
    "parts": [
        {"mimeType": "text/plain", "body": {"data": ""}},
        {"filename": "lab-certificate.png", "mimeType": "image/png",
         "body": {"attachmentId": "att-1", "size": 49000}},
        {"filename": "image001.png", "mimeType": "image/png",
         "headers": [{"name": "Content-ID", "value": "<sig>"}, {"name": "Content-Disposition", "value": "inline"}],
         "body": {"attachmentId": "att-2", "size": 900}},
    ],
}


def test_gmail_attachment_meta_carries_what_a_fetch_needs():
    meta = gmail._attachment_meta(PAYLOAD)
    assert [m["name"] for m in meta] == ["lab-certificate.png", "image001.png"]
    assert meta[0] == {"name": "lab-certificate.png", "content_type": "image/png", "size": 49000,
                       "attachment_id": "att-1", "inline": False}
    assert meta[1]["inline"] is True
    # a Content-ID alone does not make an attachment inline (Gmail stamps one on real files)
    meta2 = gmail._attachment_meta({"parts": [{"filename": "cert.png", "mimeType": "image/png",
                                              "headers": [{"name": "Content-ID", "value": "<x>"},
                                                          {"name": "Content-Disposition", "value": "attachment; filename=cert.png"}],
                                              "body": {"attachmentId": "a", "size": 50092}}]})
    assert meta2[0]["inline"] is False


def test_gmail_get_attachment_decodes_base64url(monkeypatch):
    async def fake_get(access, path, params=None, client=None):
        assert path == "/messages/m1/attachments/att-1"
        return {"data": base64.urlsafe_b64encode(b"PNG bytes").decode().rstrip("=")}
    monkeypatch.setattr(gmail, "_get", fake_get)
    assert asyncio.run(gmail.get_attachment("tok", "m1", "att-1")) == b"PNG bytes"


def test_worth_keeping_skips_inline_art_and_oversize():
    assert documents.worth_keeping("lab-certificate.png", "image/png", 49000)
    assert not documents.worth_keeping("image001.png", "image/png", 900)
    assert not documents.worth_keeping("cert.pdf", "application/pdf", 900, inline=True)
    assert documents.worth_keeping("lab-certificate-the-byre.png", "image/png", 50092, inline=True)
    assert not documents.worth_keeping("logo.png", "image/png", 900)
    assert not documents.worth_keeping("big.pdf", "application/pdf", documents.MAX_BYTES + 1)
    assert not documents.worth_keeping("pixel.gif", "image/gif", 40)


def test_object_path_is_tenant_scoped_and_safe():
    path = documents.object_path("t1", "Kris@X.co", "../lab certificate (1).png")
    assert path.startswith("t1/kris@x.co/")
    assert path.endswith("-lab certificate -1-.png")
    assert ".." not in path


def test_save_email_attachments_keeps_the_real_file_only(monkeypatch):
    fetched, stored, events, traced = [], [], [], []
    from app import trace
    monkeypatch.setattr(trace, "emit", lambda t, kind, **k: traced.append((k.get("outcome"), (k.get("detail") or {}).get("name"))))

    async def fetch(tenant_id, message_id, att, mailbox_id=None):
        fetched.append(att["name"]); return b"bytes"
    monkeypatch.setattr(mailbox, "fetch_attachment", fetch)
    monkeypatch.setattr(documents._db, "crm_contact_by_email", lambda t, e: {"id": "c1"})
    monkeypatch.setattr(documents._db, "log_agent_event", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(documents, "store", lambda t, **kw: stored.append(kw) or {"id": "d1", "filename": kw["filename"]})
    kept = asyncio.run(documents.save_email_attachments(
        "t1", mailbox_id=None, message_id="m1", email_log_id="log1", from_address="kris@x.co",
        attachments=gmail._attachment_meta(PAYLOAD)))
    assert fetched == ["lab-certificate.png"]
    assert stored[0]["contact_email"] == "kris@x.co" and stored[0]["contact_id"] == "c1"
    assert stored[0]["email_log_id"] == "log1" and stored[0]["source"] == "email"
    assert [k["filename"] for k in kept] == ["lab-certificate.png"]
    assert events and "Kept 1 document" in str(events)
    assert traced == [("kept", "lab-certificate.png"), ("skipped", "image001.png")]


def test_a_fetch_failure_never_breaks_the_rest(monkeypatch):
    async def fetch(tenant_id, message_id, att, mailbox_id=None):
        raise RuntimeError("provider down")
    monkeypatch.setattr(mailbox, "fetch_attachment", fetch)
    traced = []
    from app import trace
    monkeypatch.setattr(trace, "emit", lambda t, kind, **k: traced.append(k.get("outcome")))
    kept = asyncio.run(documents.save_email_attachments(
        "t1", mailbox_id=None, message_id="m1", email_log_id="log1", from_address="kris@x.co",
        attachments=[{"name": "a.pdf", "content_type": "application/pdf", "size": 10, "attachment_id": "x"}]))
    assert kept == []
    assert traced == ["fetch_failed"]


def test_context_lines_name_the_files_on_record():
    line = documents.context_lines([{"filename": "lab-certificate.png", "label": "Last lab certificate"},
                                    {"filename": "vet.pdf", "label": None}])
    assert "lab-certificate.png (Last lab certificate); vet.pdf" in line
    assert documents.context_lines([]) == ""
