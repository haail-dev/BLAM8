"""Documents on the CRM record — the operator's routes (Kris 2026-09-04).
Route-level via TestClient, db and storage monkeypatched, no network."""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import documents
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


DOC = {"id": "d1", "tenant_id": "t1", "filename": "lab-certificate.png", "url_or_path": "t1/kris@x.co/abc-lab-certificate.png",
       "content_type": "image/png", "size_bytes": 49000, "artifact_type": "attachment", "label": None}


def test_open_redirects_to_a_signed_url(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_document", lambda i, t, client=None: dict(DOC))
    monkeypatch.setattr(documents, "signed_url", lambda p, ttl=600, client=None: "https://signed.example/" + p)
    r = client.get("/app/crm/documents/d1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://signed.example/t1/kris@x.co/abc-lab-certificate.png"


def test_open_of_a_missing_or_foreign_document_is_404(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_document", lambda i, t, client=None: None)
    assert client.get("/app/crm/documents/nope", follow_redirects=False).status_code == 404


def test_upload_keeps_the_file_on_the_record(client, monkeypatch):
    stored = {}
    monkeypatch.setattr(dashboard.db, "crm_get_contact", lambda i, t, client=None: {"id": "c1", "email": "kris@x.co", "name": "Kris"})
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)

    def store(tenant_id, **kw):
        stored.update(kw); return {"id": "d2", "filename": kw["filename"]}
    monkeypatch.setattr(documents, "store", store)
    r = client.post("/app/crm/contacts/c1/documents",
                    files={"file": ("vet-report.pdf", b"%PDF-1.4 ...", "application/pdf")},
                    data={"label": "Vet report", "instance_id": "i-murphy"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/app/crm/contacts/c1?msg=document")
    assert stored["filename"] == "vet-report.pdf" and stored["source"] == "upload"
    assert stored["contact_email"] == "kris@x.co" and stored["contact_id"] == "c1"
    assert stored["label"] == "Vet report" and stored["instance_id"] == "i-murphy"


def test_upload_without_a_file_bounces(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "crm_get_contact", lambda i, t, client=None: {"id": "c1", "email": "kris@x.co"})
    r = client.post("/app/crm/contacts/c1/documents", data={"label": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?err=nofile")


def test_update_labels_and_pins(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.db, "get_document", lambda i, t, client=None: dict(DOC))
    monkeypatch.setattr(dashboard.db, "update_document", lambda i, t, patch, client=None: seen.update(patch))
    r = client.post("/app/crm/documents/d1/update",
                    data={"label": "Last lab certificate", "instance_id": "i-byre", "booking_id": "",
                          "return_to": "/app/crm/contacts/c1"}, follow_redirects=False)
    assert r.status_code == 303
    assert seen == {"label": "Last lab certificate", "instance_id": "i-byre", "booking_id": None,
                    "requirement_keys": []}


def test_delete_removes_the_object_and_the_row(client, monkeypatch):
    gone = []
    monkeypatch.setattr(dashboard.db, "get_document", lambda i, t, client=None: dict(DOC))
    monkeypatch.setattr(dashboard.db, "delete_document", lambda i, t, client=None: gone.append(("row", i)))
    monkeypatch.setattr(documents, "remove_object", lambda p, client=None: gone.append(("obj", p)))
    r = client.post("/app/crm/documents/d1/delete", data={"return_to": "/app/crm/contacts/c1"}, follow_redirects=False)
    assert r.status_code == 303
    assert gone == [("obj", DOC["url_or_path"]), ("row", "d1")]


def test_templates_carry_the_fold_and_the_block():
    from pathlib import Path
    card = Path("src/app/templates/app/crm_contact.html").read_text(encoding="utf-8")
    assert "face('Documents'" in card and "/documents\" enctype=\"multipart/form-data\"" in card
    job = Path("src/app/templates/app/diary_job.html").read_text(encoding="utf-8")
    assert "job_documents" in job and ">Documents</h2>" in job
