"""EU AI Act disclosure helper (src/app/disclosure.py) — the single wording source
for every auto-sent outbound. If these strings change, the pipeline, the
missed-call text-back and all proactive jobs change together."""
from app import disclosure


def test_email_footer_default_on():
    out = disclosure.wrap({}, "Hello there.")
    assert out.startswith("Hello there.")
    assert out.endswith("This automated message was written by an AI assistant on our behalf.")
    assert "\n\n--\n" in out


def test_reply_kind_matches_pipeline_wording():
    out = disclosure.wrap({}, "Thanks!", kind="reply")
    assert out.endswith("This automated reply was written by an AI assistant on our behalf.")


def test_sms_one_liner():
    out = disclosure.wrap({}, "See you at 3.", channel="sms")
    assert out == "See you at 3.\n(Automated message from our AI assistant)"


def test_sms_reply_kind():
    out = disclosure.wrap({}, "Got it.", channel="sms", kind="reply")
    assert out.endswith("(Automated reply from our AI assistant)")


def test_toggle_off_returns_text_unchanged():
    assert disclosure.wrap({"ai_disclosure": False}, "Raw body.") == "Raw body."
    assert disclosure.wrap({"ai_disclosure": False}, "Raw.", channel="sms") == "Raw."


def test_none_cfg_defaults_on():
    assert "AI assistant" in disclosure.wrap(None, "Body.")


def test_trailing_whitespace_stripped_before_footer():
    out = disclosure.wrap({}, "Body.\n\n\n")
    assert out.startswith("Body.\n\n--\n")
