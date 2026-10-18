"""Generic ignoring by kind (src/app/inbox_rules.py — Kris 2026-09-13: "do we also
provide generic email ignorance, with certain email types? … add it into
Inbox settings so that it can be changed to suit").

Run: python -m pytest tests/test_inbox_rules.py -q
"""
from pathlib import Path

from app import inbox_rules

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_switches_default_on_and_junk_reads_as_the_default():
    assert inbox_rules.settings_of(None) == {"noreply": True, "bulk": True, "auto": True}
    assert inbox_rules.settings_of({"inbox_ignore": {"bulk": False}}) == {"noreply": True, "bulk": False, "auto": True}
    assert inbox_rules.settings_of({"inbox_ignore": "nonsense"})["bulk"] is True
    assert inbox_rules.settings_of({"inbox_ignore": {"bulk": "off", "nope": False}})["bulk"] is True


def test_each_kind_is_caught_and_each_switch_turns_it_off():
    person = {"from_address": "sam@example.com", "headers": {}}
    assert inbox_rules.automated_reason(person) is None
    noreply = {"from_address": "no-reply@accounts.google.com", "headers": {}}
    assert "no-reply" in inbox_rules.automated_reason(noreply)
    assert inbox_rules.automated_reason(noreply, {"noreply": False, "bulk": True, "auto": True}) is None
    bounce = {"from_address": "mailer-daemon@googlemail.com"}
    assert inbox_rules.automated_reason(bounce)
    newsletter = {"from_address": "hello@somebrand.com", "headers": {"list-unsubscribe": "<mailto:u@x.com>"}}
    assert "unsubscribe" in inbox_rules.automated_reason(newsletter)
    assert inbox_rules.automated_reason(newsletter, {"noreply": True, "bulk": False, "auto": True}) is None
    listmail = {"from_address": "team@somebrand.com", "headers": {"precedence": "Bulk"}}
    assert "Precedence: bulk" in inbox_rules.automated_reason(listmail)
    receipt = {"from_address": "receipts@shop.com", "headers": {"auto-submitted": "auto-generated"}}
    assert "Auto-Submitted: auto-generated" in inbox_rules.automated_reason(receipt)
    assert inbox_rules.automated_reason(receipt, {"noreply": True, "bulk": True, "auto": False}) is None
    assert inbox_rules.automated_reason({"from_address": "a@b.com", "headers": {"auto-submitted": "no"}}) is None
    assert inbox_rules.automated_reason({"from_address": "a@b.com", "headers": "junk"}) is None


def test_headers_are_picked_from_both_readers_shapes():
    gmail_style = [("Subject", "Hi"), ("List-Unsubscribe", "<https://x/u>"), ("Precedence", "bulk"), ("X-Other", "y")]
    assert inbox_rules.pick_headers(gmail_style) == {"list-unsubscribe": "<https://x/u>", "precedence": "bulk"}
    graph_style = [{"name": "Auto-Submitted", "value": "auto-replied"}, {"name": "Date", "value": "x"}]
    assert inbox_rules.pick_headers(graph_style) == {"auto-submitted": "auto-replied"}
    assert inbox_rules.pick_headers(None) == {} and inbox_rules.pick_headers([1, None]) == {}


def test_the_readers_the_pipeline_the_backlog_and_settings_carry_the_rules():
    gmail = _read("src/app/gmail.py")
    assert gmail.count('"headers": _rule_headers(m),') == 2
    mailbox = _read("src/app/mailbox.py")
    assert mailbox.count('"headers": _rule_headers(m),') == 2 and mailbox.count("internetMessageHeaders") >= 2
    pipe = _read("src/app/pipeline.py")
    assert "from app.inbox_rules import is_no_reply_address" in pipe
    assert "auto_reason = inbox_rules.automated_reason(email, _inbox_rules(tenant_id))" in pipe
    assert 'category_reason=auto_reason)' in pipe
    assert '"headers": msg.get("headers") or {},     # the inbox rules read these' in pipe
    backlog = _read("src/app/backlog.py")
    assert 'category="automated" if auto_reason else "fyi",' in backlog
    assert 'category_reason=(f"Before BLAM8 · {auto_reason}" if auto_reason else REASON))' in backlog
    settings = _read("src/app/templates/app/settings.html")
    assert '<details id="inbox-rules" class="{{ subfold }}">' in settings
    assert '{{ face("Automated mail", inbox_rules_summary) }}' in settings
    assert 'name="inbox_rules_present" value="1"' in settings and 'name="ignore_{{ key }}"' in settings
    dash = _read("src/app/dashboard.py")
    assert 'if form.get("inbox_rules_present"):' in dash
    assert 'cfg["inbox_ignore"] = {key: form.get("ignore_" + key) is not None for key in inbox_rules.RULES}' in dash
    assert "**_inbox_rules_ctx(cfg)," in dash
    assert "add column if not exists inbox_ignore jsonb not null default '{}'::jsonb" in _read("db/schema.sql")


def test_the_settings_face_reads_the_state():
    from app import dashboard
    assert dashboard._inbox_rules_ctx({})["inbox_rules_summary"] == "No-reply, bulk and auto-generated mail filed, never answered"
    assert dashboard._inbox_rules_ctx({"inbox_ignore": {"noreply": False, "bulk": False, "auto": False}})["inbox_rules_summary"].startswith("Off")
    part = dashboard._inbox_rules_ctx({"inbox_ignore": {"bulk": False}})["inbox_rules_summary"]
    assert part.startswith("no-reply and bounce senders, auto-generated mail")
