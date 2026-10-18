"""Every output_config json_schema in the app must stay inside the
structured-output schema subset.

The API rejects validation keywords (maxItems, minimum, pattern, ...) in an
output_config schema with a 400 on EVERY call — the whole feature ships broken
while tests stay green. It has now happened twice: the concierge business mode
(maxItems, 2026-07-25, 0cec322 — guarded concierge schemas only) and voice
learning (minimum/maximum on formality_level, found live 2026-08-02: every
voice-learning call since 07-25 had failed in ~300ms with stop_reason
api_error). This walks EVERY schema the app passes to output_config, so the
next new module is covered the day it's written.

Tool input_schema is NOT restricted the same way (scheduling's offer_slots
tool carries minItems/maxItems/format and passed live e2e 2026-07-31), so
tool schemas are deliberately out of scope here.
"""
import pytest

from app.agent import DRAFT_SCHEMA, OUTBOUND_SCHEMA
from app.agents.field_service import FSM_SCHEMA
from app.agents.tms import TMS_SCHEMA
from app.assets_condition import DIFF_SCHEMA
from app.concierge import (BUSINESS_SCHEMA, CONCIERGE_SCHEMA, FIX_SCHEMA,
                           HELP_SCHEMA, ONBOARDING_STEPS, onboarding_schema)
from app.crm_extract import EXTRACT_SCHEMA
from app import events_os
from app.crm_intel import _analyze_schema, _INTEL_EXTENSIONS
from app.party_extract import EXTRACT_SCHEMA as PARTY_EXTRACT_SCHEMA
from app.portal_chat import PORTAL_SCHEMA
from app.practice_noshow import NO_SHOW_SCHEMA
from app.shop_ocr import OCR_SCHEMA
from app.voice_learning import VOICE_PROFILE_SCHEMA

BANNED = {"maxItems", "minItems", "minLength", "maxLength", "pattern",
          "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
          "multipleOf", "format", "uniqueItems", "minProperties",
          "maxProperties"}


def walk(node, path=""):
    bad = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in BANNED:
                bad.append(f"{path}.{k}")
            bad += walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            bad += walk(v, f"{path}[{i}]")
    return bad


ALL_SCHEMAS = {
    "agent.DRAFT_SCHEMA": DRAFT_SCHEMA,
    "agent.OUTBOUND_SCHEMA": OUTBOUND_SCHEMA,
    "field_service.FSM_SCHEMA": FSM_SCHEMA,
    "tms.TMS_SCHEMA": TMS_SCHEMA,
    "assets_condition.DIFF_SCHEMA": DIFF_SCHEMA,
    "concierge.CONCIERGE_SCHEMA": CONCIERGE_SCHEMA,
    "concierge.HELP_SCHEMA": HELP_SCHEMA,
    "concierge.FIX_SCHEMA": FIX_SCHEMA,
    "concierge.BUSINESS_SCHEMA": BUSINESS_SCHEMA,
    "crm_extract.EXTRACT_SCHEMA": EXTRACT_SCHEMA,
    "party_extract.EXTRACT_SCHEMA": PARTY_EXTRACT_SCHEMA,
    "portal_chat.PORTAL_SCHEMA": PORTAL_SCHEMA,
    "practice_noshow.NO_SHOW_SCHEMA": NO_SHOW_SCHEMA,
    "shop_ocr.OCR_SCHEMA": OCR_SCHEMA,
    "voice_learning.VOICE_PROFILE_SCHEMA": VOICE_PROFILE_SCHEMA,
}
ALL_SCHEMAS.update({f"concierge.onboarding_schema[{step}]": onboarding_schema(step)
                    for step in ONBOARDING_STEPS})
ALL_SCHEMAS.update({f"crm_intel._analyze_schema[{ev},{se}]": _analyze_schema((_INTEL_EXTENSIONS["event"],) if ev else (), se)
                    for ev in (False, True) for se in (False, True)})


@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_output_schema_stays_inside_the_structured_output_subset(name):
    bad = walk(ALL_SCHEMAS[name])
    assert not bad, (f"{name} uses keywords the structured-output subset rejects "
                     f"(400 on every call): {bad} — enforce these limits in code "
                     f"instead, like voice_learning._merge clamps formality_level.")
