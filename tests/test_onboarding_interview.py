"""Interview-first onboarding (the assistant-led front door).

The wizard's write paths are exercised end-to-end elsewhere; these pin the
step order, the industry-value composition shared by the interview and the
classic picker, and the classic picker borrowing the interview's stepper slot.
"""
from app import onboarding
from app.onboarding import STEPS, _industry_value, _step_ctx


def test_interview_is_the_front_door():
    assert STEPS == ["interview", "mailbox", "voice", "knowledge"]
    assert "industry" not in STEPS                     # classic picker: route kept, slot gone
    assert "data" not in STEPS                         # live-data: route kept, off-flow 08-02


def test_industry_value_composition():
    # preset ids pass through; 'other' captures the free-text sector (the
    # roadmap signal), falling back to literal 'other' when none given
    assert _industry_value("logistics", "") == "logistics"
    assert _industry_value("other", " Boarding kennels ") == "Boarding kennels"
    assert _industry_value("other", "") == "other"


def test_interview_cfg_fields_capture_brief_goals_and_website():
    assert onboarding.INTERVIEW_CFG_FIELDS == ("business_brief", "goals", "website")


def test_classic_picker_borrows_the_interview_stepper_slot():
    ctx = _step_ctx("industry")
    assert ctx["step"] == "industry"                   # widget still runs the industry brain
    assert ctx["stepper"] == "interview"               # progress bar highlights step 1
    assert ctx["step_index"] == 0

    ctx2 = _step_ctx("voice")
    assert ctx2["stepper"] == "voice"
    assert ctx2["step_index"] == STEPS.index("voice")
