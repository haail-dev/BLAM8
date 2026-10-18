"""Per-business Concierge task timings: the settings card and its save route."""
import app.dashboard as dashboard
from tests.test_draft_hold import client, _settings_env  # noqa: F401  (fixture + page env)


def _page(monkeypatch, stored):
    _settings_env(monkeypatch, hold=False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "google", "mailbox": "hello@acme.co.uk", "portal_slug": "acme",
        "concierge_settings": stored})


def _fold(html):
    return html.split('data-fold="Concierge tasks"', 1)[1].split("</details>", 1)[0]


def test_card_shows_defaults_as_placeholders_not_values(client, monkeypatch):
    _page(monkeypatch, {})
    fold = _fold(client.get("/app/settings").text)
    assert "Default timings" in client.get("/app/settings").text
    assert 'name="task_nudge_minutes"' in fold and 'placeholder="30"' in fold
    assert 'placeholder="24"' in fold and 'placeholder="5"' in fold
    assert 'value=""' in fold and 'value="30"' not in fold


def test_card_shows_what_the_business_chose(client, monkeypatch):
    _page(monkeypatch, {"task_nudge_minutes": 45, "task_expire_minutes": 720})
    html = client.get("/app/settings").text
    fold = _fold(html)
    assert "Custom timings" in html
    assert 'value="45"' in fold and 'value="12"' in fold


def test_save_stores_only_clamped_values(client, monkeypatch):
    saved = []
    monkeypatch.setattr(dashboard.db, "set_concierge_settings", lambda tid, values, **k: saved.append((tid, values)))
    r = client.post("/app/settings/concierge",
                    data={"task_nudge_minutes": "1", "task_expire_hours": "12", "task_stall_minutes": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert saved == [("t1", {"task_nudge_minutes": 5, "task_expire_minutes": 720})]
    client.post("/app/settings/concierge", data={}, follow_redirects=False)
    assert saved[-1] == ("t1", {})
