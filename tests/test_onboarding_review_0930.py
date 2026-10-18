"""The fixes from watching the onboarding film run (docs/onboarding-review-2026-09-30.md).

P1: a first enquiry without an address made the draft loop write the same
    availability claim twice (offer_slots refused for missing details, the
    reservation guard rejected the draft, the nudge asked for the impossible).
P3: a held setting showed its stored code ("windows ampm").
P4: the Knowledge step claimed a website read with no site on file.
P5: a knowledge-gap question became a source title, cut mid-sentence.

Run: python -m pytest tests/test_onboarding_review_0930.py -q
"""
import pathlib

from app import scheduling, setup_held
from app.templating import gap_title

ROOT = pathlib.Path(__file__).parent.parent


# ── P1 ────────────────────────────────────────────────────────────────────────
def test_missing_info_refusal_forbids_availability_talk():
    out = scheduling._missing_info(["Job address", "What needs doing"])
    assert out["error"] == "missing_info"
    assert "say NOTHING about availability, days, times or slots" in out["message"]
    assert "the times come next" in out["message"]
    # the earlier instructions survive
    assert "Do not book yet" in out["message"] and "never tell the customer something went wrong" in out["message"]


def test_reserve_nudge_after_an_intake_refusal_asks_for_the_details_not_a_reservation(monkeypatch):
    """offer_slots refused for missing details, then a draft that names availability:
    the nudge must say what the model CAN do (drop the claim, ask for the details)
    and must not force another diary call."""
    from tests.test_agent_tools import run_draft, text_response, tool_response
    executed = []

    def execute(name, args):
        executed.append(name)
        if name == "offer_slots":
            return scheduling._missing_info(["Job address", "What needs doing"])
        return {"ok": True}

    fake, result = run_draft(monkeypatch, [
        tool_response("offer_slots", {"slots": [{"date": "2026-10-05", "window": "am"}], "service": "Repair"},
                      tool_id="tu-1"),
        # names a choice of times with nothing reserved: the reservation guard's trigger
        text_response({"reply": "We have 09:00 or 10:00 available next week. Could you send your address?",
                       "deferred": False, "missing": "", "action_summary": "Offered morning slots"}),
        text_response({"reply": "Before I can book a morning, could you send your full address and what the tap is doing?",
                       "deferred": False, "missing": ""}),
    ], execute, scheduling=True)
    assert executed == ["offer_slots"]
    def words(content):
        if isinstance(content, str):
            return content
        return " ".join(str(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")) for b in content)
    stops = [words(m["content"]) for m in fake.calls[-1]["messages"]
             if m.get("role") == "user" and words(m["content"]).startswith("STOP")]
    assert len(stops) == 1
    assert "refused because the customer has not given: Job address, What needs doing" in stops[0]
    assert "Do NOT call the diary tools again" in stops[0]
    assert "reserves NOTHING" not in stops[0]          # the old, impossible order
    assert "Before I can book a morning" in result.text


# ── P3 ────────────────────────────────────────────────────────────────────────
def test_held_setting_codes_read_as_words():
    # worded "and" since 906ec0f1 (30 Sep), when the receipt and the card took one vocabulary
    assert setup_held.describe("diary_settings", "windows", "ampm") == "morning and afternoon windows"
    assert setup_held.describe("diary_settings", "windows", "slots") == "timed slots"
    # unknown values keep the plain form the older tests pin
    assert setup_held.describe("diary_settings", "capacity_per_window", "30") == "capacity per window 30"


# ── P4 ────────────────────────────────────────────────────────────────────────
def test_knowledge_step_only_claims_a_website_read_with_a_site_on_file():
    k = (ROOT / "src/app" / "templates" / "onboarding" / "knowledge.html").read_text(encoding="utf-8")
    assert "{% if site_url %}We read your website and the assistant asks for the rest{% else %}" in k
    assert "The assistant asks for what's missing, or paste a note{% endif %}" in k


# ── P5 ────────────────────────────────────────────────────────────────────────
def test_gap_question_becomes_a_short_source_title():
    assert gap_title("whether wet underfloor heating installation is a service the business "
                     "offers and its pricing") == "Wet underfloor heating installation is a service"
    assert gap_title("what the cancellation fee is") == "The cancellation fee"
    assert gap_title("Parking near the house") == "Parking near the house"
    assert gap_title("") == ""
    t = (ROOT / "src/app" / "templates" / "app" / "_thread.html").read_text(encoding="utf-8")
    assert 'value="{{ pending.knowledge_gap | gap_title }}"' in t
    assert "truncate(80" not in t
