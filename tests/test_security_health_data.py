"""Health data in CRM fields (security review 2026-09, findings M39 + M41).

M39: a special-category field (health / medical / allergy) is never filled or
suggested from email, never probed for, and never put into the drafter's
customer context. Presets mark such seeds "sensitive"; a stored definition
(which has no column for the flag) is matched back to the preset by key, and
an owner's own field is caught by a generic label fallback.

M41: the platform KB tells the truth about what is read, kept and reused.

Run: python -m pytest tests/test_security_health_data.py -q
"""
from app import crm_fields, crm_intel, industry_lingo, platform_kb_content


def _stored(label, entity="contact", field_type="text", **extra):
    """A definition as it comes back from crm_field_definitions — no flag."""
    return {"id": f"d-{crm_fields.slug(label)}", "entity": entity,
            "key": crm_fields.slug(label), "label": label, "field_type": field_type,
            "options": [], "extraction_hint": "x", **extra}


MEDICAL = _stored("Allergies / medical", probe_behaviour="probe_once")
ROLE = _stored("Role", probe_behaviour="probe_once")


# ── the preset flag ──────────────────────────────────────────────────────────

def test_every_health_seed_is_flagged_and_its_hint_is_honest():
    flagged = industry_lingo.sensitive_seed_fields()
    assert ("contact", "Allergies / medical") in flagged
    for seeds in industry_lingo.CRM_SEEDS.values():
        for fields in seeds.values():
            for f in fields:
                hint = f.get("extraction_hint") or ""
                assert "routes to encrypted notes" not in hint
                if "allerg" in (f["label"] + hint).lower():
                    assert f.get("sensitive") is True, f["label"]


def test_is_special_category():
    assert crm_fields.is_special_category({"label": "Anything", "sensitive": True})
    assert crm_fields.is_special_category(MEDICAL)            # matched back to the preset
    assert crm_fields.is_special_category(_stored("Dietary notes", entity="deal"))
    assert crm_fields.is_special_category(_stored("Health conditions"))   # owner's own field
    assert crm_fields.is_special_category(_stored("Medication"))
    assert not crm_fields.is_special_category(ROLE)
    assert not crm_fields.is_special_category(_stored("Insurer"))
    # a date says when, not the fact: a certificate date keeps its autofill
    assert not crm_fields.is_special_category(_stored("Medical expiry", field_type="date"))
    assert not crm_fields.is_special_category(None)


# ── never auto-filled or suggested ───────────────────────────────────────────

def test_plan_skips_a_health_field_at_any_confidence():
    plan = crm_fields.plan_application([MEDICAL, ROLE], [
        {"entity": "contact", "key": MEDICAL["key"], "value": "on warfarin", "confidence": 0.99},
        {"entity": "contact", "key": MEDICAL["key"], "value": "penicillin", "confidence": 0.6},
        {"entity": "contact", "key": "role", "value": "Owner", "confidence": 0.9},
    ])
    assert [(p["definition"]["key"], p["action"], p["value"]) for p in plan] == [
        (MEDICAL["key"], "skip", None), (MEDICAL["key"], "skip", None),
        ("role", "autofill", "Owner")]


def test_apply_extraction_never_writes_or_suggests_a_health_field(monkeypatch):
    merged, suggested = [], []
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True:
                        merged.append(values) or {})
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda tid, **kw: suggested.append(kw) or True)
    monkeypatch.setattr(crm_fields.db, "crm_pending_suggestions", lambda *a, **k: [])
    tally = crm_fields.apply_extraction(
        "t1", {"contact": "ct1"}, [MEDICAL, ROLE],
        [{"entity": "contact", "key": MEDICAL["key"], "value": "on warfarin", "confidence": 0.95},
         {"entity": "contact", "key": MEDICAL["key"], "value": "latex", "confidence": 0.6},
         {"entity": "contact", "key": "role", "value": "Owner", "confidence": 0.9}], "log1")
    assert merged == [{"role": "Owner"}]
    assert suggested == []
    assert tally == {"autofilled": 1, "suggested": 0, "skipped": 2}


# ── never probed for, never in the drafter's prompt ──────────────────────────

def test_the_agent_never_asks_for_a_health_field(monkeypatch):
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions", lambda *a, **k: [MEDICAL, ROLE])
    monkeypatch.setattr(crm_fields.db, "crm_get_contact",
                        lambda *a, **k: {"id": "ct1", "custom_data": {}})
    monkeypatch.setattr(crm_fields.db, "crm_get_company", lambda *a, **k: None)
    monkeypatch.setattr(crm_fields.db, "crm_probe_log_for_contact", lambda *a, **k: [])
    missing = crm_fields.get_missing_probeable_fields("t1", "ct1")
    assert [d["key"] for d in missing] == ["role"]
    assert "Allergies" not in crm_fields.build_probe_instructions([{**MEDICAL, "_attempts": 0}])


def test_drafter_context_leaves_health_fields_out():
    contact = {"id": "ct1", "email": "a@b.co.uk", "name": "Sam",
               "custom_data": {MEDICAL["key"]: "on warfarin", "role": "Owner"},
               "crm_companies": None}
    ctx = crm_intel.build_crm_context(contact, [MEDICAL, ROLE], [], [])
    assert "Role: Owner" in ctx
    assert "warfarin" not in ctx and "Allergies" not in ctx
    # a field flagged on the definition itself is left out the same way
    flagged = {"entity": "contact", "key": "notes_x", "label": "Notes X", "sensitive": True}
    contact["custom_data"]["notes_x"] = "asthma"
    assert "asthma" not in crm_intel.build_crm_context(contact, [flagged, ROLE], [], [])


# ── M41: the KB says what really happens ─────────────────────────────────────

def test_kb_no_longer_claims_health_data_is_not_extracted():
    blob = " ".join(platform_kb_content.SOURCES.values())
    assert "not separately extracted or reused" not in blob
    entry = platform_kb_content.SOURCES["Health, allergy and other sensitive details"]
    for must in ("CRM → Fields", "Diary → Dependants", "Remove this kind", "You confirm",
                 "never filled in or suggested", "never put into a reply draft"):
        assert must in entry, must


def test_extraction_never_sends_sensitive_fields(monkeypatch):
    import asyncio
    from app import crm_extract
    seen = {}

    async def fake_create(**kw):
        seen["prompt"] = kw["messages"][0]["content"]
        raise RuntimeError("stop")
    monkeypatch.setattr(crm_extract.llm, "create", fake_create)
    defs = [{"key": "allergies", "label": "Allergies / medical", "entity": "contact", "field_type": "text", "sensitive": True},
            {"key": "gate", "label": "Gate code", "entity": "contact", "field_type": "text"}]
    try:
        asyncio.run(crm_extract.extract("t", defs, subject="s", body="b"))
    except RuntimeError:
        pass
    assert "Gate code" in seen["prompt"] and "Allergies" not in seen["prompt"]
