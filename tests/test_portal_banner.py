"""Portal banner branding (2026-08-02): upload → public bucket → /c/{slug}.

Pins:
  * magic-byte sniffing accepts png/jpeg/webp/gif and rejects everything else
    (the browser's declared content-type is never trusted)
  * upload_banner: public bucket ensured, tenant-scoped versioned path
  * settings save: upload stores the path + deletes the old object, remove
    clears it, a junk/oversize file still saves the rest of the form and
    reports a banner-only error
  * onboarding About-you: optional upload saves; invalid re-renders the step
    with an error AFTER the rest is saved; no file just moves on
  * /c/{slug}: banner img only when one is set

Run: python -m pytest tests/test_portal_banner.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.onboarding as onboarding
import app.portal as portal
from app import storage
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"0" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 16
GIF = b"GIF89a" + b"0" * 16


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def ob_client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── sniffing ─────────────────────────────────────────────────────────────────
def test_sniff_accepts_the_image_formats():
    assert storage.sniff_banner(PNG) == ("image/png", "png")
    assert storage.sniff_banner(JPG) == ("image/jpeg", "jpg")
    assert storage.sniff_banner(WEBP) == ("image/webp", "webp")
    assert storage.sniff_banner(GIF) == ("image/gif", "gif")
    assert storage.sniff_banner(b"GIF87a" + b"0" * 8) == ("image/gif", "gif")


def test_sniff_rejects_everything_else():
    assert storage.sniff_banner(b"") is None
    assert storage.sniff_banner(b"<svg onload=alert(1)>") is None     # SVG = scriptable, out
    assert storage.sniff_banner(b"%PDF-1.7 ...") is None
    assert storage.sniff_banner(b"RIFF\x00\x00\x00\x00WAVE") is None  # RIFF but not WEBP
    assert storage.sniff_banner(b"\x89PNGnope") is None               # truncated signature


# ── upload / delete / public URL against a fake Storage client ───────────────
class _FakeClient:
    def __init__(self):
        self.calls = []
        self.storage = self

    def create_bucket(self, name, options=None):
        self.calls.append(("create_bucket", name, options))

    def from_(self, bucket):
        self.calls.append(("from", bucket))
        return self

    def upload(self, path, data, opts):
        self.calls.append(("upload", path, opts))

    def remove(self, paths):
        self.calls.append(("remove", paths))

    def get_public_url(self, path):
        return f"https://cdn.example/{path}"


def test_upload_banner_public_bucket_and_versioned_path(monkeypatch):
    monkeypatch.setattr(storage, "_branding_ready", False)
    fake = _FakeClient()
    path = storage.upload_banner("t1", PNG, "image/png", "png", client=fake)
    assert path.startswith("t1/banner-") and path.endswith(".png")
    assert ("create_bucket", storage.BRANDING_BUCKET, {"public": True}) in fake.calls
    upload = next(c for c in fake.calls if c[0] == "upload")
    assert upload[1] == path and upload[2]["content-type"] == "image/png"
    # a second upload never reuses the path — replaced banners bust caches
    assert storage.upload_banner("t1", PNG, "image/png", "png", client=fake) != path


def test_upload_banner_failure_is_storage_error(monkeypatch):
    monkeypatch.setattr(storage, "_branding_ready", True)
    fake = _FakeClient()
    monkeypatch.setattr(_FakeClient, "upload",
                        lambda self, *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(storage.StorageError):
        storage.upload_banner("t1", PNG, "image/png", "png", client=fake)


def test_banner_public_url_and_delete_degrade_quietly():
    assert storage.banner_public_url(None) is None
    assert storage.banner_public_url("") is None
    assert storage.banner_public_url("t1/banner-ab.png", client=_FakeClient()) \
        == "https://cdn.example/t1/banner-ab.png"
    fake = _FakeClient()
    storage.delete_banner("", client=fake)          # no-op, no calls
    assert not fake.calls
    storage.delete_banner("t1/banner-ab.png", client=fake)
    assert ("remove", ["t1/banner-ab.png"]) in fake.calls


# ── settings save ────────────────────────────────────────────────────────────
def _settings_env(monkeypatch, old_path="t1/banner-old.png"):
    saved, deleted = {}, []
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"name": "Acme", "portal_slug": "acme"})
    monkeypatch.setattr(dashboard.db, "get_tenant_config",
                        lambda *a, **k: {"portal_banner_path": old_path})
    monkeypatch.setattr(storage, "upload_banner",
                        lambda tid, data, ctype, ext, **k: f"{tid}/banner-new.{ext}")
    monkeypatch.setattr(storage, "delete_banner", lambda p, **k: deleted.append(p))
    return saved, deleted


def test_settings_banner_upload_saves_path_and_deletes_old(client, monkeypatch):
    saved, deleted = _settings_env(monkeypatch)
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "acme"},
                      files={"portal_banner": ("b.png", PNG, "image/png")},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert res.status_code == 303 and "portal=saved" in res.headers["location"]
    assert saved["portal_banner_path"] == "t1/banner-new.png"
    assert deleted == ["t1/banner-old.png"]


def test_settings_junk_banner_keeps_the_rest_of_the_form(client, monkeypatch):
    saved, deleted = _settings_env(monkeypatch)
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "acme",
                            "portal_description": "Boilers"},
                      files={"portal_banner": ("evil.svg", b"<svg/>", "image/png")},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "portal=banner_invalid" in res.headers["location"]
    assert "portal_banner_path" not in saved       # banner untouched…
    assert saved["portal_description"] == "Boilers"  # …but the form saved
    assert not deleted


def test_settings_oversize_banner_is_rejected(client, monkeypatch):
    saved, _ = _settings_env(monkeypatch)
    monkeypatch.setattr(storage, "MAX_BANNER_BYTES", 10)
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "acme"},
                      files={"portal_banner": ("b.png", PNG, "image/png")},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "portal=banner_too_big" in res.headers["location"]
    assert "portal_banner_path" not in saved


def test_settings_storage_outage_is_a_message_not_a_500(client, monkeypatch):
    saved, _ = _settings_env(monkeypatch)
    monkeypatch.setattr(storage, "upload_banner",
                        lambda *a, **k: (_ for _ in ()).throw(storage.StorageError("down")))
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "acme"},
                      files={"portal_banner": ("b.png", PNG, "image/png")},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "portal=banner_storage" in res.headers["location"]
    assert "portal_banner_path" not in saved


def test_settings_remove_tick_clears_the_banner(client, monkeypatch):
    saved, deleted = _settings_env(monkeypatch)
    res = client.post("/app/settings/portal",
                      data={"csrf_token": "x", "portal_slug": "acme",
                            "portal_banner_remove": "on"},
                      cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert "portal=saved" in res.headers["location"]
    assert saved["portal_banner_path"] is None
    assert deleted == ["t1/banner-old.png"]


# ── onboarding About-you ─────────────────────────────────────────────────────
def _interview_env(monkeypatch):
    saved, deleted = {}, []
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    monkeypatch.setattr(onboarding.db, "get_tenant_config",
                        lambda *a, **k: {"portal_banner_path": None})
    monkeypatch.setattr(storage, "upload_banner",
                        lambda tid, data, ctype, ext, **k: f"{tid}/banner-new.{ext}")
    monkeypatch.setattr(storage, "delete_banner", lambda p, **k: deleted.append(p))
    return saved, deleted


def test_onboarding_banner_upload_saves_and_continues(ob_client, monkeypatch):
    saved, _ = _interview_env(monkeypatch)
    res = ob_client.post("/onboarding/interview",
                         data={"csrf_token": "x"},
                         files={"portal_banner": ("van.jpg", JPG, "image/jpeg")},
                         cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/onboarding/mailbox"
    assert saved["portal_banner_path"] == "t1/banner-new.jpg"


def test_onboarding_invalid_banner_rerenders_step_with_the_rest_saved(ob_client, monkeypatch):
    saved, _ = _interview_env(monkeypatch)
    res = ob_client.post("/onboarding/interview",
                         data={"csrf_token": "x", "business_brief": "We fix boilers"},
                         files={"portal_banner": ("cv.pdf", b"%PDF-1.7", "image/png")},
                         cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert res.status_code == 303
    assert "banner_err=invalid" in res.headers["location"]
    assert saved["business_brief"] == "We fix boilers"
    assert "portal_banner_path" not in saved


def test_onboarding_no_file_just_moves_on(ob_client, monkeypatch):
    saved, _ = _interview_env(monkeypatch)
    res = ob_client.post("/onboarding/interview", data={"csrf_token": "x"},
                         cookies={"bl_csrf": "x"}, follow_redirects=False)
    assert res.headers["location"] == "/onboarding/mailbox"
    assert "portal_banner_path" not in saved


# ── /c/{slug} rendering ──────────────────────────────────────────────────────
def _page_env(monkeypatch, cfg):
    tenant = {"id": "t1", "name": "Acme Plumbing", "portal_slug": "acme-plumbing"}
    monkeypatch.setattr(portal.db, "get_tenant_by_portal_slug",
                        lambda slug: dict(tenant) if slug == tenant["portal_slug"] else None)
    monkeypatch.setattr(portal.db, "get_tenant_config", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(portal.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(portal.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda *a, **k: None)


def test_portal_page_shows_banner_when_set(client, monkeypatch):
    _page_env(monkeypatch, {"portal_enabled": True,
                            "portal_banner_path": "t1/banner-ab12.png"})
    monkeypatch.setattr(storage, "banner_public_url",
                        lambda p, **k: f"https://cdn.example/{p}" if p else None)
    res = client.get("/c/acme-plumbing")
    assert res.status_code == 200
    assert "https://cdn.example/t1/banner-ab12.png" in res.text
    assert "object-cover" in res.text


def test_portal_page_without_banner_has_no_img(client, monkeypatch):
    _page_env(monkeypatch, {"portal_enabled": True})
    res = client.get("/c/acme-plumbing")
    assert res.status_code == 200
    assert "cdn.example" not in res.text
    assert 'alt=""' not in res.text          # the banner img isn't rendered at all
