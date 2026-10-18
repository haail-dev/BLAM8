"""The voice step's card can fill `voice_notes` (run #6, 2026-09-04).

Three runs running (farrier, hot tub, garage) the owner gave standing
instructions at the voice step — "always say waiting vs leave-it-with-us",
"never a repair price before we've seen the car" — and the assistant said it
had "baked them in" while voice_notes stayed empty: the step's schema had no
such field, so the rules were folded into `tone`. The schema names the field
the page already has, and tells the model what belongs there.

Run: python -m pytest tests/test_onboarding_voice_notes_field.py -q
"""
from app import concierge, onboarding


def test_voice_step_schema_names_every_voice_field_the_page_saves():
    fields = concierge.ONBOARDING_STEPS["voice"]["fields"]
    assert "voice_notes" in fields
    for f in onboarding.VOICE_FIELDS:
        if f == "business_brief":
            # Saved by the page but prefilled from step 1 and deliberately NOT a
            # voice-step fill since 60b2691 (the assistant re-wrote it every turn).
            continue
        assert f in fields, f


def test_guidance_routes_standing_instructions_to_voice_notes():
    g = concierge.ONBOARDING_STEPS["voice"]["guidance"]
    assert "voice_notes" in g and "tone" in g
