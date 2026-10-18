"""Voice learning as a pending onboarding step, and the Examples removal
(Kris 2026-09-01).

Voice is one of onboarding.STEPS, and it is the step most likely to be left
undone: a new tenant often has too little sent mail for the learner to work
with, so the run comes back 'insufficient' and nothing ever mentions it again.
Settings now carries it as unfinished setup until it is either done or
dismissed — and dismissing is a real, reversible decision of its own rather than
a status value pretending the work happened.

Examples went at the same time ("Remove the Examples part completely"). The
field is NOT gone from the database: voice-apply still fills it from the learned
sample and agent.build_system_prompt still reads it. What went is the textarea —
so the two things worth pinning are the traps that removing a form field opens,
both of which would corrupt data silently rather than fail.

Run: python -m pytest tests/test_voice_onboarding_step.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, cfg=None, saved=None):
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: dict(cfg or {}))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "google", "mailbox": "hello@acme.co.uk", "portal_slug": "acme"})
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: {"sub_expires": None})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.portal, "ensure_portal_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(dashboard.storage, "banner_public_url", lambda *a, **k: None)
    if saved is not None:
        monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                            lambda tid, cfg, **k: saved.update(cfg))


def _voice_row(html):
    return html.split('data-fold="Voice auto-learning"', 1)[1].split("</details>", 1)[0]


# ── Unfinished setup speaks up ─────────────────────────────────────────────
@pytest.mark.parametrize("cfg, why", [
    ({}, "never run"),
    ({"voice_learning_status": "insufficient"}, "not enough sent mail to learn from"),
    ({"voice_learning_status": "failed"}, "the run broke"),
])
def test_an_unfinished_voice_step_is_flagged_and_opens_itself(client, monkeypatch, cfg, why):
    _env(monkeypatch, cfg)
    html = client.get("/app/settings").text
    row = _voice_row(html)
    assert "data-needs" in row, f"unflagged when {why}"
    assert "Setup step not finished" in row
    # everything else on this page that wants you opens itself; so does this
    opening_tag = html[:html.index('data-fold="Voice auto-learning"')].rsplit("<details", 1)[1]
    assert " open" in opening_tag, f"the row stayed shut when {why}"


@pytest.mark.parametrize("cfg", [
    {"voice_profile_source": "auto_learned"},
    {"voice_profile_source": "hybrid"},
])
def test_a_finished_step_says_nothing(client, monkeypatch, cfg):
    _env(monkeypatch, cfg)
    row = _voice_row(client.get("/app/settings").text)
    assert "data-needs" not in row
    assert "Setup step not finished" not in row


def test_skipping_silences_it_without_claiming_it_was_done(client, monkeypatch):
    """The distinction that matters: skipped is not learned. The face says
    'Skipped', not 'Learned', and the provenance is untouched."""
    _env(monkeypatch, {"voice_learning_status": "insufficient", "voice_learning_ignored": True})
    row = _voice_row(client.get("/app/settings").text)
    assert "data-needs" not in row
    assert "Skipped" in row
    assert "Learned from your sent emails" not in row
    assert "Remind me about this step" in row, "the skip has to be reversible"


def test_skip_sits_beside_discard_after_a_run(client, monkeypatch):
    """Kris: the ignore option goes "along side Discard Profile after running
    the learning" — and they are different acts, so both are offered."""
    _env(monkeypatch, {"voice_profile_source": "auto_learned", "voice_profile": {"x": 1}})
    row = _voice_row(client.get("/app/settings").text)
    assert "Discard learned profile" in row
    assert "Skip this step" in row


# ── The route behind it ────────────────────────────────────────────────────
@pytest.mark.parametrize("posted, stored", [("1", True), ("0", False)])
def test_the_skip_route_writes_only_its_own_flag(client, monkeypatch, posted, stored):
    """Its own column, never voice_learning_status: that records what the
    LEARNER did, and faking 'accepted' to silence the prompt would claim a
    profile exists when none does."""
    saved = {}
    _env(monkeypatch, {}, saved=saved)
    r = client.post("/app/settings/voice-ignore", data={"ignored": posted},
                    follow_redirects=False)
    assert r.status_code == 303
    assert saved == {"voice_learning_ignored": stored}


# ── Examples: gone from the page, safe in the database ─────────────────────
def test_the_examples_editor_is_gone(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert 'data-fold="Examples"' not in html
    assert 'name="examples"' not in html


def test_removing_the_editor_cannot_wipe_what_the_learner_wrote(monkeypatch):
    """The save handler blanks every SETTINGS_FIELD missing from the form. Left
    in that tuple, `examples` would be written as None on every save — quietly
    deleting the sample voice-apply had put there, which agent.py still reads
    into the prompt."""
    assert "examples" not in dashboard.SETTINGS_FIELDS


def test_the_missing_field_cannot_relabel_a_learned_voice_as_hand_edited(monkeypatch):
    """The hybrid check compares submitted voice fields against stored ones. With
    `examples` in that tuple but absent from the form, None != the stored text on
    EVERY save, silently restamping an auto_learned voice as 'hybrid'."""
    import inspect
    src = inspect.getsource(dashboard.save_settings)
    voice_keys = src.split("voice_keys = (", 1)[1].split(")", 1)[0]
    assert "examples" not in voice_keys


# ── The sign-off is saved in one canonical form (src/app/sign_off.py) ──────────
def test_settings_save_stores_the_sign_off_in_one_canonical_form(client, monkeypatch):
    """A browser textarea posts CRLF and stray spaces; the stored value is LF, trimmed,
    so the value agent.py and the Concierge cards append never carries a CR."""
    saved = {}
    _env(monkeypatch, {}, saved)
    r = client.post("/app/settings", data={"sign_off": "Kind regards,\r\nThe team \r\n"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert saved["sign_off"] == "Kind regards,\nThe team"
    saved.clear()
    client.post("/app/settings", data={"sign_off": " \r\n "}, follow_redirects=False)
    assert saved["sign_off"] is None            # blank = nothing configured


def test_a_no_op_settings_save_keeps_auto_learned_provenance(client, monkeypatch):
    """Saving an old CRLF value unchanged is not a hand-edit: the learned label stays."""
    saved = {}
    stored = {"voice_profile_source": "auto_learned", "tone": "warm",
              "sign_off": "Cheers,\r\nSam"}
    _env(monkeypatch, stored, saved)
    client.post("/app/settings", data={"tone": "warm", "sign_off": "Cheers,\r\nSam"},
                follow_redirects=False)
    assert saved["sign_off"] == "Cheers,\nSam"
    assert "voice_profile_source" not in saved
    # A real change to the sign-off still relabels it as hand-adjusted.
    saved.clear()
    client.post("/app/settings", data={"tone": "warm", "sign_off": "Thanks,\r\nSam"},
                follow_redirects=False)
    assert saved["voice_profile_source"] == "hybrid"


def test_mailbox_save_stores_the_sign_off_in_one_canonical_form(client, monkeypatch):
    _env(monkeypatch, {})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "list_mailboxes",
                        lambda *a, **k: [{"id": "mb-1", "address": "team@example.invalid"}])
    monkeypatch.setattr(dashboard.db, "update_mailbox_meta", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "module_ui_visible", lambda *a, **k: False)
    saved = []
    monkeypatch.setattr(dashboard.db, "upsert_mailbox_config",
                        lambda mailbox_id, tenant_id, cfg, client=None: saved.append(cfg))
    r = client.post("/app/mailboxes/mb-1/settings",
                    data={"sign_off": "Cheers,\r\nThe team \r\n"}, follow_redirects=False)
    assert r.status_code == 303
    assert saved[-1]["sign_off"] == "Cheers,\nThe team"
    client.post("/app/mailboxes/mb-1/settings", data={"sign_off": "  \r\n"}, follow_redirects=False)
    assert saved[-1]["sign_off"] is None        # blank = inherit the company sign-off
