import json
"""CRM custom fields — phase 2 (src/app/crm_fields.py, src/app/crm_extract.py, the
/app/crm/fields wizard + suggestion confirm routes).

Unit: slugging/validation, the 10-per-entity cap, typed coercion, the §10
confidence gate (autofill / suggest / skip), plan_application's
downgrade-when-uncoercible rule, seeds minus already-defined, accept/dismiss.
Extraction: prompt build + response parsing with the Anthropic call mocked.
Routes: wizard page CRUD + the About card + suggestion chips, TestClient with
db monkeypatched (house Env pattern).

Run: python -m pytest tests/test_crm_fields.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import agent, crm, crm_extract, crm_fields, crm_intel, db, industry_lingo
from app import knowledge as kb
from app import pipeline, usage
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── Definitions: slug / validate / cap ───────────────────────────────────────

def test_slug():
    assert crm_fields.slug("Fleet size") == "fleet_size"
    assert crm_fields.slug("  Credit limit (£)! ") == "credit_limit"
    assert crm_fields.slug("???") == ""


def test_validate_definition_shapes_and_rejects():
    d = crm_fields.validate_definition("contact", " Fleet size ", "number", None,
                                       " vans and trucks ")
    assert d == {"entity": "contact", "key": "fleet_size", "label": "Fleet size",
                 "field_type": "number", "options": [], "required": False,
                 "extraction_hint": "vans and trucks", "probe_behaviour": "passive",
                 "durability": "once", "recall_days": 30, "valid_months": 0}
    with pytest.raises(crm_fields.FieldError):
        crm_fields.validate_definition("contact", "", "text", None, "")
    with pytest.raises(crm_fields.FieldError):
        crm_fields.validate_definition("contact", "X", "wibble", None, "")
    with pytest.raises(crm_fields.FieldError):
        crm_fields.validate_definition("sideways", "X", "text", None, "")
    with pytest.raises(crm_fields.FieldError):   # dropdown needs 2+ options
        crm_fields.validate_definition("deal", "Type", "dropdown", ["only"], "")


def test_add_field_enforces_cap_and_duplicates(monkeypatch):
    existing = [{"entity": "contact", "key": f"f{i}", "label": f"F{i}"} for i in range(10)]
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: list(existing))
    with pytest.raises(crm_fields.FieldError, match="max 10"):
        crm_fields.add_field("t1", {"entity": "contact", "key": "new", "label": "New"})
    existing[:] = [{"entity": "contact", "key": "fleet_size", "label": "Fleet size"}]
    with pytest.raises(crm_fields.FieldError, match="already track"):
        crm_fields.add_field("t1", {"entity": "contact", "key": "fleet_size",
                                    "label": "Fleet size"})


def test_seeds_for_tenant_skips_what_exists_and_ignores_the_industry_label():
    defined = [{"entity": "company", "key": "location"}]
    seeds = crm_fields.seeds_for_tenant({"industry": "logistics"}, defined)
    assert "location" not in [crm_fields.slug(s["label"]) for s in seeds["company"]]
    assert seeds["contact"]                        # untouched entities still suggest
    for industry in ("logistics", "kennels", "a made-up sector", None):
        assert crm_fields.seeds_for_tenant({"industry": industry}, []) == crm_fields.seeds_for_tenant({}, [])
    every = set(industry_lingo.CRM_SEEDS) | {None}
    for ind in every:                              # every seed ships valid
        for entity, items in industry_lingo.crm_seeds_for(ind).items():
            for s in items:
                crm_fields.validate_definition(entity, s["label"], s["field_type"],
                                               s.get("options"), s["extraction_hint"])


# ── Coercion + the confidence gate ───────────────────────────────────────────

def test_coerce_value_types():
    assert crm_fields.coerce_value("number", "12 vans") == (False, None)
    assert crm_fields.coerce_value("number", "£1,200") == (True, 1200)
    assert crm_fields.coerce_value("number", "2.5") == (True, 2.5)
    assert crm_fields.coerce_value("bool", "Yes") == (True, True)
    assert crm_fields.coerce_value("bool", "nope") == (False, None)
    assert crm_fields.coerce_value("date", "delivery on 2026-08-01 please") == (True, "2026-08-01")
    assert crm_fields.coerce_value("date", "next Tuesday") == (False, None)
    assert crm_fields.coerce_value("dropdown", "perm", ["Perm", "Contract"]) == (True, "Perm")
    assert crm_fields.coerce_value("dropdown", "freelance", ["Perm", "Contract"]) == (False, None)
    assert crm_fields.coerce_value("text", "  Transport manager ") == (True, "Transport manager")
    assert crm_fields.coerce_value("text", None) == (False, None)


def test_gate_thresholds():
    assert crm_fields.gate(0.9) == "autofill"
    assert crm_fields.gate(0.75) == "autofill"
    assert crm_fields.gate(0.6) == "suggest"
    assert crm_fields.gate(0.5) == "suggest"
    assert crm_fields.gate(0.4) == "skip"
    assert crm_fields.gate(None) == "skip"
    assert crm_fields.gate("not a number") == "skip"


DEFS = [
    {"entity": "company", "key": "fleet_size", "label": "Fleet size",
     "field_type": "number", "options": [], "extraction_hint": "vans"},
    {"entity": "contact", "key": "role", "label": "Role",
     "field_type": "text", "options": [], "extraction_hint": "job title"},
]


def test_plan_application_gates_and_downgrades():
    plan = crm_fields.plan_application(DEFS, [
        {"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.9},
        {"entity": "contact", "key": "role", "value": "Transport manager", "confidence": 0.6},
        {"entity": "company", "key": "fleet_size", "value": "a dozen", "confidence": 0.9},
        {"entity": "contact", "key": "role", "value": "Guessing", "confidence": 0.2},
        {"entity": "deal", "key": "invented_by_model", "value": "x", "confidence": 0.99},
    ])
    actions = [(p["definition"]["key"], p["action"], p["value"]) for p in plan]
    assert actions[0] == ("fleet_size", "autofill", 12)
    assert actions[1] == ("role", "suggest", "Transport manager")
    # High-confidence but un-coercible ("a dozen" as a number) → downgraded to
    # a suggestion carrying the raw text, never silently trusted.
    assert actions[2] == ("fleet_size", "suggest", "a dozen")
    assert actions[3] == ("role", "skip", "Guessing")
    assert len(actions) == 4                       # the invented field vanished


def test_apply_extraction_writes_suggests_and_counts(monkeypatch):
    merged, suggested = [], []
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True:
                        merged.append((entity, rid, values)) or {})
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda tid, **kw: suggested.append(kw) or True)
    tally = crm_fields.apply_extraction(
        "t1", {"company": "co1", "contact": "ct1"}, DEFS,
        [{"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.9},
         {"entity": "contact", "key": "role", "value": "Owner", "confidence": 0.6},
         {"entity": "contact", "key": "role", "value": "x", "confidence": 0.1}],
        "log1")
    assert tally == {"autofilled": 1, "suggested": 1, "skipped": 1}
    assert merged == [("company", "co1", {"fleet_size": 12})]
    assert suggested[0]["field_key"] == "role" and suggested[0]["email_log_id"] == "log1"


def test_an_autofill_that_would_replace_becomes_a_suggestion(monkeypatch):
    """Live 2026-09-04: a landlord's SECOND property overwrote the customer's own
    boiler make on the record. Extraction reads one email and cannot know which
    of the customer's things it was about, and the value was TRUE — just true
    about something else — so no confidence gate could catch it. A contradiction
    is now offered, not applied."""
    suggested = []
    # The db layer declines the write and reports what is already stored.
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True:
                        {} if overwrite else {"fleet_size": 40})
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda tid, **kw: suggested.append(kw) or True)
    tally = crm_fields.apply_extraction(
        "t1", {"company": "co1"}, DEFS,
        [{"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.95}],
        "log1")
    # High confidence, and still not written: it queues for the human instead.
    assert tally == {"autofilled": 0, "suggested": 1, "skipped": 0}
    assert suggested[0]["field_key"] == "fleet_size" and suggested[0]["value"] == 12


def test_an_autofill_into_an_empty_slot_still_just_writes(monkeypatch):
    """The guard must not turn every autofill into homework — an empty field
    fills silently, exactly as before."""
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True: {})
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda tid, **kw: pytest.fail("nothing to confirm"))
    tally = crm_fields.apply_extraction(
        "t1", {"company": "co1"}, DEFS,
        [{"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.95}],
        "log1")
    assert tally == {"autofilled": 1, "suggested": 0, "skipped": 0}


def test_builtin_phone_fills_the_first_class_column_only(monkeypatch):
    """The built-in _phone target (Julius 2026-09-03: a verified customer was
    asked for a number the business had been given twice): autofill goes to
    crm_set_contact_phone_if_empty — never custom_data, never a suggestion
    chip — and a low-confidence read just skips."""
    filled, merged = [], []
    monkeypatch.setattr(crm_fields.db, "crm_set_contact_phone_if_empty",
                        lambda rid, tid, phone, client=None:
                        filled.append((rid, phone)))
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda *a, **k: merged.append(a))
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda *a, **k: pytest.fail("phone never suggests"))
    defs = DEFS + crm_fields.BUILTIN_DEFINITIONS
    tally = crm_fields.apply_extraction(
        "t1", {"contact": "ct1"}, defs,
        [{"entity": "contact", "key": "_phone", "value": "07495 111222",
          "confidence": 0.95},
         {"entity": "contact", "key": "_phone", "value": "01234",
          "confidence": 0.2}],
        "log1")
    assert filled == [("ct1", "07495 111222")]
    assert merged == []                       # first-class column, not custom_data
    assert tally["autofilled"] == 1 and tally["skipped"] == 1


def test_apply_extraction_ignores_entities_without_a_record(monkeypatch):
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda *a, **k: pytest.fail("must not write"))
    tally = crm_fields.apply_extraction(
        "t1", {"contact": "ct1"}, DEFS,      # freemail sender: no company record
        [{"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.9}],
        "log1")
    assert tally == {"autofilled": 0, "suggested": 0, "skipped": 0}


# ── Suggestions: accept / dismiss ────────────────────────────────────────────

def test_accept_suggestion_coerces_and_resolves(monkeypatch):
    s = {"id": "sg1", "status": "pending", "entity": "company", "record_id": "co1",
         "field_key": "fleet_size", "value": "12"}
    merged, resolved = [], []
    monkeypatch.setattr(crm_fields.db, "crm_get_suggestion",
                        lambda sid, tid, client=None: dict(s))
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: list(DEFS))
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None:
                        merged.append(values))
    monkeypatch.setattr(crm_fields.db, "crm_resolve_suggestion",
                        lambda sid, tid, status, client=None: resolved.append(status))
    assert crm_fields.accept_suggestion("t1", "sg1") is True
    assert merged == [{"fleet_size": 12}] and resolved == ["accepted"]
    s["field_key"] = "deleted_since"               # definition gone → auto-dismiss
    merged.clear(); resolved.clear()
    assert crm_fields.accept_suggestion("t1", "sg1") is False
    assert merged == [] and resolved == ["dismissed"]


# ── Extraction: prompt + parsing (Anthropic mocked) ──────────────────────────

def test_build_user_prompt_carries_definitions_and_email():
    p = crm_extract.build_user_prompt(DEFS, "3 pallets", "we run 12 vans", "Rob")
    assert "key=fleet_size" in p and "type=number" in p and "— vans" in p
    assert "Subject: 3 pallets" in p and "we run 12 vans" in p and "Rob" in p


def test_parse_response_is_tolerant():
    good = '{"fields": [{"entity": "company", "key": "fleet_size", "value": "12", "confidence": 0.9}]}'
    assert crm_extract.parse_response(good)[0]["key"] == "fleet_size"
    assert crm_extract.parse_response("not json at all") == []
    assert crm_extract.parse_response('{"fields": "weird"}') == []


@pytest.mark.asyncio
async def test_extract_calls_haiku_and_meters_usage(monkeypatch):
    calls, metered = [], []

    class Msg:
        content = [type("B", (), {"type": "text", "text":
                    '{"fields": [{"entity": "contact", "key": "role", '
                    '"value": "Owner", "confidence": 0.8}]}'})()]
        usage = {"input_tokens": 100, "output_tokens": 20}
        model = "claude-haiku-4-5-20251001"

    class FakeMessages:
        async def create(self, **kw):
            calls.append(kw)
            return Msg()

    monkeypatch.setattr(crm_extract.agent, "client",
                        lambda: type("C", (), {"messages": FakeMessages()})())
    monkeypatch.setattr(crm_extract.usage, "record_anthropic",
                        lambda *a, **k: metered.append((a, k)))
    out = await crm_extract.extract("t1", DEFS, subject="s", body="b",
                                    email_log_id="log1")
    assert out[0]["value"] == "Owner"
    assert calls[0]["output_config"]["format"]["schema"] == crm_extract.EXTRACT_SCHEMA
    assert metered[0][1]["operation"] == "crm_extract"
    assert await crm_extract.extract("t1", [], subject="s", body="b") == []


# ── Routes: wizard + About card + suggestion chips ───────────────────────────

@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class Env:
    def __init__(self, monkeypatch, *, defs=(), suggestions=(), industry="logistics"):
        self.defs = list(defs)
        self.suggestions = list(suggestions)
        self.deleted, self.resolved = [], []
        def snapshot(ctx, page):
            return {'page': page, 'definitions': [dict(d) for d in self.defs]}
        monkeypatch.setattr(dashboard.setup_forms, 'load', snapshot)
        def save(ctx, page, expected, operations):
            assert expected == snapshot(ctx, page)
            for op in operations:
                if op['op'] == 'insert':
                    self.defs.append({**op['fields'], 'id': op['id']})
                elif op['op'] == 'update':
                    dashboard.db.crm_update_field_definition(op['id'], ctx.tenant_id, op['fields'])
                else:
                    self.deleted.append(op['id'])
        monkeypatch.setattr(dashboard.setup_forms, 'save', save)
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})   # CRM unlocks at Core (2026-07-24)
        monkeypatch.setattr(dashboard.db, "get_tenant",
                            lambda *a, **k: {"name": "Koth", "industry": industry})
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "crm_list_field_definitions",
                            lambda tid, entity=None, client=None:
                            [d for d in self.defs if entity in (None, d["entity"])])
        monkeypatch.setattr(dashboard.db, "crm_insert_field_definition",
                            lambda tid, d, client=None: self.defs.append(
                                {**d, "id": f"fd{len(self.defs)}"}) or self.defs[-1])
        monkeypatch.setattr(dashboard.db, "crm_delete_field_definition",
                            lambda did, tid, client=None: self.deleted.append(did))
        monkeypatch.setattr(dashboard.db, "crm_pending_suggestions",
                            lambda tid, record_ids=None, client=None: list(self.suggestions))
        monkeypatch.setattr(dashboard.db, "crm_get_contact",
                            lambda cid, tid, client=None: {
                                "id": cid, "email": "rob@acme-haulage.co.uk",
                                "name": "Rob Smith", "company_id": "co1",
                                "first_seen_at": "2026-05-01T09:00:00+00:00",
                                "last_activity_at": "2026-07-05T07:00:00+00:00",
                                "custom_data": {"role": "Transport manager"},
                                "crm_companies": {"id": "co1", "name": "Acme Haulage"}})
        monkeypatch.setattr(dashboard.db, "crm_get_company",
                            lambda cid, tid, client=None: {
                                "id": "co1", "name": "Acme Haulage",
                                "custom_data": {"fleet_size": 12}})
        monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "crm_activities_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "crm_open_promises",
                            lambda tid, contact_id=None, client=None: [])
        monkeypatch.setattr(dashboard.crm_fields, "accept_suggestion",
                            lambda tid, sid, client=None: self.resolved.append(("accept", sid)))
        monkeypatch.setattr(dashboard.crm_fields, "dismiss_suggestion",
                            lambda tid, sid, client=None: self.resolved.append(("dismiss", sid)))


FIELD_DEFS = [
    {"id": "fd1", "entity": "contact", "key": "role", "label": "Role",
     "field_type": "text", "options": [], "required": False,
     "extraction_hint": "job title", "display_order": 0},
    {"id": "fd2", "entity": "company", "key": "fleet_size", "label": "Fleet size",
     "field_type": "number", "options": [], "required": False,
     "extraction_hint": "vans", "display_order": 0},
]


def test_fields_page_shows_defs_and_generic_seeds_only(client, monkeypatch):
    Env(monkeypatch, defs=FIELD_DEFS)
    r = client.get("/app/crm/fields")
    assert r.status_code == 200
    assert "Role" in r.text and "Fleet size" in r.text
    assert "+ Location" in r.text            # the generic seed is on offer
    assert "Depot count" not in r.text       # an industry label never chooses chips
    assert "+ Role" not in r.text            # already defined → chip gone
    assert "Preview with my last 5 emails" in r.text


def test_empty_fields_page_hides_inbox_preview(client, monkeypatch):
    Env(monkeypatch)
    response = client.get("/app/crm/fields")
    assert response.status_code == 200
    assert 'action="/app/crm/fields/preview"' not in response.text


def test_fields_add_validates_via_redirect(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/fields", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "entity": "contact", "label": "Phone",
                                             "field_type": "text",
                                             "extraction_hint": "numbers in signatures"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    assert env.defs[0]["key"] == "phone"
    r = client.post("/app/crm/fields", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "entity": "contact", "label": "Type",
                                             "field_type": "dropdown", "options": "one"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]  # dropdown needs 2+ options


def test_contact_page_renders_values_and_suggestion_chips(client, monkeypatch):
    Env(monkeypatch, defs=FIELD_DEFS, suggestions=[
        {"id": "sg1", "entity": "company", "record_id": "co1",
         "field_key": "fleet_size", "value": "14", "confidence": 0.6}])
    r = client.get("/app/crm/contacts/ct1")
    assert r.status_code == 200
    assert "Transport manager" in r.text            # contact custom_data value
    assert "Fleet size" in r.text and "12" in r.text  # company value inline
    assert "Fleet size = 14" in r.text              # the suggestion chip
    assert "/app/crm/suggestions/sg1/accept" in r.text


def test_contact_page_invites_setup_when_no_defs(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/contacts/ct1")
    assert "Set up your fields" in r.text


def test_suggestion_accept_and_dismiss_route(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/suggestions/sg1/accept",
                    data={"return_to": "/app/crm/contacts/ct1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/crm/contacts/ct1"
    r = client.post("/app/crm/suggestions/sg1/dismiss",
                    data={"return_to": "/app/crm/contacts/ct1"}, follow_redirects=False)
    assert env.resolved == [("accept", "sg1"), ("dismiss", "sg1")]


def test_preview_dry_runs_against_recent_inbound(client, monkeypatch):
    Env(monkeypatch, defs=FIELD_DEFS)
    monkeypatch.setattr(dashboard.db, "crm_recent_inbound",
                        lambda tid, limit=5, client=None: [
                            {"id": "e1", "from_address": "rob@acme-haulage.co.uk",
                             "from_name": "Rob", "subject": "Fleet chat",
                             "body_in": "we run 12 vans"}])
    async def fake_extract(tid, defs, **kw):
        assert kw["email_log_id"] is None           # dry run: nothing attributed
        return [{"entity": "company", "key": "fleet_size", "value": "12",
                 "confidence": 0.9}]
    monkeypatch.setattr(dashboard.crm_extract, "extract", fake_extract)
    r = client.post("/app/crm/fields/preview")
    assert r.status_code == 200
    assert "would fill in" in r.text and "Fleet size" in r.text
    assert "90% sure" in r.text

def test_a_shorter_reread_is_not_a_contradiction_and_a_chip_is_offered_once(monkeypatch):
    # Live 2026-09-04 (farrier run): "Coxley Green Livery" re-read against the stored
    # "Coxley Green Livery, just outside Wells, BA5 1RB" was offered as a replacement,
    # and the same chip was rendered once per email that re-read it.
    defs = [{"entity": "contact", "key": "yard_address", "label": "Yard address",
             "field_type": "text", "options": [], "extraction_hint": ""}]
    stored = "Coxley Green Livery, just outside Wells, BA5 1RB"
    suggested = []
    monkeypatch.setattr(crm_fields.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True:
                        {} if overwrite else {"yard_address": stored})
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_suggestion",
                        lambda tid, **kw: suggested.append(kw) or True)
    pending = []
    monkeypatch.setattr(crm_fields.db, "crm_pending_suggestions",
                        lambda tid, ids=None, client=None: list(pending))

    # less information than the record → neither written nor offered
    tally = crm_fields.apply_extraction("t1", {"contact": "ct1"}, defs,
        [{"entity": "contact", "key": "yard_address", "value": "Coxley Green Livery", "confidence": 0.95}], "e1")
    assert suggested == [] and tally["skipped"] == 1

    # a genuinely different value is offered — once
    new = "Hollow Farm, Priddy, Wells BA5 3BP"
    crm_fields.apply_extraction("t1", {"contact": "ct1"}, defs,
        [{"entity": "contact", "key": "yard_address", "value": new, "confidence": 0.95}], "e2")
    assert [s["value"] for s in suggested] == [new]
    pending.append({"field_key": "yard_address", "value": new, "status": "pending"})
    tally = crm_fields.apply_extraction("t1", {"contact": "ct1"}, defs,
        [{"entity": "contact", "key": "yard_address", "value": "  hollow farm, priddy, wells ba5 3bp ", "confidence": 0.95}], "e3")
    assert [s["value"] for s in suggested] == [new] and tally["skipped"] == 1
    assert crm_fields.loses_information("Coxley Green Livery", stored)
    assert not crm_fields.loses_information(stored, "Coxley Green Livery")   # more information is a real change
    assert not crm_fields.loses_information("", stored)


def test_the_contact_card_moves_a_customer_ladder(monkeypatch):
    """POST /app/crm/contacts/{id}/level: the owner's control for a CUSTOMER
    ladder (run #8: a driving pupil has no dependant, so nothing else could
    move the level). Writes custom_data, fires the contact-side level hook,
    and lands back on the card with the notify-mode flash."""
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        merged, fired = [], []
        defs = [{"key": "dvsa", "label": "DVSA driver's record", "entity": "contact", "field_type": "dropdown",
                 "options": ["Introduced", "Helped", "Prompted"], "ladder": {"notify": "approve", "gates": []}},
                {"key": "phone", "label": "Phone", "entity": "contact", "field_type": "text", "options": []}]
        monkeypatch.setattr(dashboard.db, "crm_get_contact",
                            lambda cid, tid, client=None: {"id": cid, "name": "Kris", "custom_data": {"dvsa": "Helped"}})
        monkeypatch.setattr(dashboard.db, "crm_list_field_definitions", lambda tid, entity=None, client=None: defs)
        monkeypatch.setattr(dashboard.db, "crm_merge_custom_data",
                            lambda entity, rid, tid, data, client=None: merged.append((entity, rid, data)))
        from app import ladders
        monkeypatch.setattr(ladders, "contact_field_changed",
                            lambda tid, contact, key, old, new, client=None: fired.append((key, old, new)) or True)
        c = TestClient(app)
        r = c.post("/app/crm/contacts/c-1/level", data={"key": "dvsa", "level": "Prompted"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/crm/contacts/c-1?msg=level_queued"
        assert merged == [("contact", "c-1", {"dvsa": "Prompted"})]
        assert fired == [("dvsa", "Helped", "Prompted")]
        # a level that is not on the ladder, or a field that is not a ladder: nothing written
        for bad in ({"key": "dvsa", "level": "Expert"}, {"key": "phone", "level": "Prompted"}):
            r = c.post("/app/crm/contacts/c-1/level", data=bad, follow_redirects=False)
            assert r.headers["location"].endswith("?msg=badlevel")
        assert len(merged) == 1
    finally:
        app.dependency_overrides.clear()


def test_the_about_rows_carry_the_ladder_levels():
    defs = [{"key": "dvsa", "label": "DVSA", "entity": "contact", "field_type": "dropdown",
             "options": ["Introduced", "Helped"], "ladder": {"notify": "approve", "gates": []}},
            {"key": "phone", "label": "Phone", "entity": "contact", "field_type": "text", "options": []}]
    rows = dashboard._crm_card_fields(defs, "contact", {"custom_data": {"dvsa": "Helped"}})
    assert rows[0]["ladder"] == ["Introduced", "Helped"] and rows[0]["raw"] == "Helped" and rows[0]["key"] == "dvsa"
    assert rows[1]["ladder"] is None


def test_a_level_field_beside_a_ladder_is_refused(monkeypatch):
    """Run #8: "Current DVSA skill level | text" beside the "DVSA driver's record"
    ladder — two fields for one fact. A level/stage/grade field is refused when
    a ladder already sits on the customer record; any other field still adds."""
    existing = [{"entity": "contact", "key": "dvsa_driver_s_record", "label": "DVSA driver's record",
                 "ladder": {"notify": "approve", "gates": []}}]
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: list(existing))
    inserted = []
    monkeypatch.setattr(crm_fields.db, "crm_insert_field_definition",
                        lambda tid, d, client=None: inserted.append(d) or d)
    with pytest.raises(crm_fields.FieldError, match="progress level"):
        crm_fields.add_field("t1", {"entity": "contact", "key": "current_dvsa_skill_level",
                                    "label": "Current DVSA skill level"})
    crm_fields.add_field("t1", {"entity": "contact", "key": "test_centre", "label": "Test centre"})
    assert [d["label"] for d in inserted] == ["Test centre"]
    # no ladder on record: a "level" field is the owner's to add
    existing[:] = [{"entity": "contact", "key": "phone", "label": "Phone"}]
    crm_fields.add_field("t1", {"entity": "contact", "key": "cefr_level", "label": "CEFR level"})
    assert inserted[-1]["label"] == "CEFR level"


def test_the_card_parser_reads_dropdown_options_and_falls_back_to_text():
    from pathlib import Path
    js = Path(__file__).resolve().parents[1].joinpath("src", "app", "static", "concierge.js").read_text(encoding="utf-8")
    assert "dropdown: Hereford, Worcester" in js and 'if (type === "dropdown" && options.length < 2) type = "text";' in js
    assert 'options: options.join(",")' in js
    # the outcome of every row reaches the page: ?msg=saved&added=N and the refusals in ?err=
    assert 'if (q.get("msg") === "saved") added += 1;' in js
    assert 'qs.set("err", errs.join(" · "))' in js and 'redirect: "manual"' not in js.split("crm_fields")[1][:4000]


def test_a_bare_day_month_lands_in_the_year_the_sender_meant():
    """Run #8: "My test is booked for 14 December" was extracted as 2024-12-14.
    A date the email never gave a year for is re-yeared: the next such date for
    a field that looks ahead, the nearest otherwise; a stated year is kept."""
    from datetime import date
    today = date(2026, 9, 5)
    defs = [{"entity": "contact", "key": "test_date", "label": "Test date", "field_type": "date",
             "durability": "expires"},
            {"entity": "contact", "key": "last_service", "label": "Boiler last serviced",
             "field_type": "date", "durability": "once"},
            {"entity": "contact", "key": "dob", "label": "Date of birth", "field_type": "date"},
            {"entity": "contact", "key": "role", "label": "Role", "field_type": "text"}]
    fields = [{"entity": "contact", "key": "test_date", "value": "2024-12-14", "confidence": 1},
              {"entity": "contact", "key": "last_service", "value": "2024-06-03", "confidence": 1},
              {"entity": "contact", "key": "dob", "value": "1990-12-14", "confidence": 1},
              {"entity": "contact", "key": "role", "value": "2024-12-14", "confidence": 1}]
    text = "My test is booked for 14 December at Hereford. The boiler was last done on 3 June. DOB 14/12/1990."
    out = crm_extract.resolve_date_years(defs, fields, text, today=today)
    vals = {f["key"]: f["value"] for f in out}
    assert vals["test_date"] == "2026-12-14"       # ahead: the next 14 December
    assert vals["last_service"] == "2026-06-03"    # nearest 3 June to today (95 days back, not 270 ahead)
    assert vals["dob"] == "1990-12-14"             # the email states the year
    assert vals["role"] == "2024-12-14"            # not a date field
    # a stated year in any form is kept; an ahead date already past rolls to next year
    kept = crm_extract.resolve_date_years(defs, [{"entity": "contact", "key": "test_date",
                                                  "value": "2024-12-14", "confidence": 1}],
                                          "passed on 14/12/24", today=today)
    assert kept[0]["value"] == "2024-12-14"
    nxt = crm_extract.resolve_date_years(defs, [{"entity": "contact", "key": "test_date",
                                                 "value": "2026-03-03", "confidence": 1}],
                                         "test on 3 March", today=today)
    assert nxt[0]["value"] == "2027-03-03"
    prompt = crm_extract.build_user_prompt(defs, "s", "b", today=today)
    assert prompt.startswith("Today is 2026-09-05 (Saturday). A date the email gives WITHOUT a year")


def test_a_placeholder_is_never_a_stated_name_or_company():
    """Run #8: a degenerate "..." output filed "..." as the customer's company
    and wrote it into _company_provenance."""
    from app import crm
    from app.agent import _parse_draft
    out = _parse_draft('{"reply":"hi","contact_name":"...","contact_company":"…"}')
    assert out["contact_name"] == "" and out["contact_company"] == ""
    assert crm._said("...") is None and crm._said("n/a") is None and crm._said(" Acme Ltd ") == "Acme Ltd"
    assert crm._stated_name({"contact_name": "..."}) is None



def test_validate_definition_carries_probe_behaviour():
    for behaviour in crm_fields.PROBE_BEHAVIOURS:
        d = crm_fields.validate_definition("contact", "Budget", "number", None, "",
                                           probe_behaviour=behaviour)
        assert d["probe_behaviour"] == behaviour
    with pytest.raises(crm_fields.FieldError, match="probe behaviour"):
        crm_fields.validate_definition("contact", "Budget", "number", None, "",
                                       probe_behaviour="pester_forever")
    # Every persistent behaviour has a cap — 'ask forever' must be impossible.
    for behaviour in crm_fields.PROBE_BEHAVIOURS:
        if behaviour != "passive":
            assert crm_fields.MAX_PROBE_ATTEMPTS[behaviour] >= 1


def test_fields_add_persists_probe_behaviour(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/fields", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "entity": "contact", "label": "Budget",
                                             "field_type": "number",
                                             "probe_behaviour": "probe_once"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    assert env.defs[0]["probe_behaviour"] == "probe_once"
    # The wizard round-trips: the stored behaviour comes back selected.
    r = client.get("/app/crm/fields")
    assert 'value="probe_once" selected' in r.text
    # An invented behaviour bounces with the validation message.
    r = client.post("/app/crm/fields", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "entity": "contact", "label": "X",
                                             "probe_behaviour": "shout"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert len(env.defs) == 1


def test_fields_edit_updates_probe_behaviour(client, monkeypatch):
    Env(monkeypatch, defs=FIELD_DEFS)
    updated = []
    monkeypatch.setattr(dashboard.db, "crm_update_field_definition",
                        lambda did, tid, fields, client=None: updated.append((did, fields)))
    r = client.post("/app/crm/fields/fd1",
                    data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "label": "Role", "probe_behaviour": "probe_persistent"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    assert updated[0][0] == "fd1"
    assert updated[0][1]["probe_behaviour"] == "probe_persistent"
    r = client.post("/app/crm/fields/fd1",
                    data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None, "crm_fields")), "label": "Role", "probe_behaviour": "shout"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"] and len(updated) == 1


# ── Probing (Phase 3.5 §18.2): missing-field selection + the attempt cap ─────

PROBE_DEF = {"id": "fd-budget", "entity": "contact", "key": "budget",
             "label": "Budget", "field_type": "number", "options": [],
             "required": False, "extraction_hint": "rough spend",
             "probe_behaviour": "probe_once", "display_order": 0}


def _probe_env(monkeypatch, *, defs, custom_data=None, log_rows=()):
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: list(defs))
    monkeypatch.setattr(crm_fields.db, "crm_get_contact",
                        lambda cid, tid, client=None:
                        {"id": cid, "custom_data": dict(custom_data or {})})
    monkeypatch.setattr(crm_fields.db, "crm_get_company",
                        lambda cid, tid, client=None: {"id": cid, "custom_data": {}})
    monkeypatch.setattr(crm_fields.db, "crm_probe_log_for_contact",
                        lambda tid, cid: list(log_rows))


def test_missing_probeable_skips_passive_and_filled(monkeypatch):
    _probe_env(monkeypatch, defs=[
        PROBE_DEF,
        {**PROBE_DEF, "id": "fd-role", "key": "role", "probe_behaviour": "passive"},
        {**PROBE_DEF, "id": "fd-area", "key": "area"},
    ], custom_data={"area": "Leeds"})
    out = crm_fields.get_missing_probeable_fields("t1", "ct1")
    # passive never probes; a filled value needs no question — only budget is left
    assert [f["id"] for f in out] == ["fd-budget"]


def test_probe_cap_engages_once_attempts_are_logged(monkeypatch):
    one_attempt = [{"field_definition_id": "fd-budget", "attempt_number": 1}]
    _probe_env(monkeypatch, defs=[PROBE_DEF], log_rows=one_attempt)
    # probe_once: a single logged attempt exhausts the field
    assert crm_fields.get_missing_probeable_fields("t1", "ct1") == []
    # probe_persistent: 3 attempts allowed, so one leaves it live…
    persistent = {**PROBE_DEF, "probe_behaviour": "probe_persistent"}
    _probe_env(monkeypatch, defs=[persistent], log_rows=one_attempt)
    out = crm_fields.get_missing_probeable_fields("t1", "ct1")
    assert [f["_attempts"] for f in out] == [1]
    # …and 3 exhausts it
    _probe_env(monkeypatch, defs=[persistent], log_rows=one_attempt * 3)
    assert crm_fields.get_missing_probeable_fields("t1", "ct1") == []


def test_log_probe_attempt_writes_the_row(monkeypatch):
    rows = []
    monkeypatch.setattr(crm_fields.db, "crm_insert_probe_log",
                        lambda tid, **kw: rows.append((tid, kw)))
    crm_fields.log_probe_attempt("t1", "ct1", "fd-budget", draft_id="log-1")
    assert rows == [("t1", {"contact_id": "ct1", "field_definition_id": "fd-budget",
                            "draft_id": "log-1"})]


# ── Pipeline wiring: a probing draft logs the attempt (so the cap can fire) ──

class _ProbePipelineEnv:
    """test_crm_gating's _PipelineEnv, with a real CRM contact so the drafter
    context — and its PROBE block — actually builds. Records the probe rows
    the pipeline logs and the crm_context the drafter received."""

    def __init__(self, monkeypatch, *, created=True, result=None):
        self.probe_rows = []
        self.crm_context = None
        self.contexts = []
        self.drafts = []

        async def fake_draft(cfg, email, **kw):
            self.crm_context = kw.get("crm_context")
            self.contexts.append(self.crm_context)
            reply = ("What is your budget?" if "PROBE:" in (self.crm_context or "") else "Reply.")
            self.drafts.append(reply)
            return result or agent.DraftResult(text=reply, usage={"input_tokens": 1}, model="m")

        async def fake_retrieve(*a, **k):
            return []

        async def fake_noop(*a, **k):
            return {}

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
        monkeypatch.setattr(kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(kb, "render_for_prompt", lambda *a, **k: None)
        monkeypatch.setattr(db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "own_mailbox_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"industry": "logistics"})
        monkeypatch.setattr(db, "effective_config", lambda *a, **k: {})
        monkeypatch.setattr(db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(db, "active_modules", lambda *a, **k: {"crm"})
        monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("log-1", created))
        monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(crm, "ingest_email_log_row",
                            lambda tid, row: {"contact_id": "ct1", "company_id": "co1"})
        monkeypatch.setattr(crm_extract, "extract", fake_noop)
        monkeypatch.setattr(crm_fields, "apply_extraction",
                            lambda *a, **k: {"autofilled": 0, "suggested": 0})
        monkeypatch.setattr(crm_intel, "process_email", fake_noop)
        # The CRM knows the sender, tracks one probe_once field, still blank.
        monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {
            "id": "ct1", "email": "rob@acme.co.uk", "name": "Rob Smith",
            "custom_data": {},
            "crm_companies": {"id": "co1", "name": "Acme Haulage"}})
        monkeypatch.setattr(db, "crm_list_field_definitions",
                            lambda tid, entity=None, client=None: [dict(PROBE_DEF)])
        monkeypatch.setattr(db, "crm_list_deals", lambda *a, **k: [])
        monkeypatch.setattr(db, "crm_open_promises", lambda *a, **k: [])
        monkeypatch.setattr(db, "crm_activities_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(db, "crm_get_contact",
                            lambda cid, tid, client=None: {"id": cid, "custom_data": {}})
        monkeypatch.setattr(db, "crm_get_company",
                            lambda cid, tid, client=None: {"id": cid, "custom_data": {}})
        monkeypatch.setattr(db, "crm_probe_log_for_contact",
                            lambda *a, **k: [row for tid, row in self.probe_rows if tid == a[0] and row["contact_id"] == a[1]])
        monkeypatch.setattr(db, "crm_insert_probe_log",
                            lambda tid, **kw: self.probe_rows.append((tid, kw)))


PROBE_EMAIL = {"from_address": "rob@acme.co.uk", "subject": "Quote", "body": "Friday?"}


def test_pipeline_logs_the_probe_attempt_against_the_draft(monkeypatch):
    env = _ProbePipelineEnv(monkeypatch)
    log_id = asyncio.run(pipeline.draft_for_email("t1", PROBE_EMAIL,
                                                  source_message_id="m1"))
    assert log_id == "log-1"
    assert "PROBE:" in env.crm_context and "Budget" in env.crm_context
    assert env.probe_rows == [("t1", {"contact_id": "ct1",
                                      "field_definition_id": "fd-budget",
                                      "draft_id": "log-1"})]


def test_pipeline_duplicate_delivery_burns_no_probe_attempt(monkeypatch):
    # A webhook redelivery (insert_email_log dedupes to created=False) drafted
    # nothing new — counting it would exhaust the cap without ever asking.
    env = _ProbePipelineEnv(monkeypatch, created=False)
    asyncio.run(pipeline.draft_for_email("t1", PROBE_EMAIL, source_message_id="m1"))
    assert env.probe_rows == []


@pytest.mark.parametrize("behaviour, expected", [("passive", 0), ("probe_once", 1), ("probe_persistent", 3)])
def test_pipeline_probe_cap_across_queued_drafts(monkeypatch, behaviour, expected):
    env = _ProbePipelineEnv(monkeypatch)
    monkeypatch.setattr(db, "crm_list_field_definitions",
                        lambda *a, **k: [{**PROBE_DEF, "probe_behaviour": behaviour}])
    for i in range(4):
        asyncio.run(pipeline.draft_for_email("t1", {**PROBE_EMAIL, "body": f"Message {i}"},
                                           source_message_id=f"m{i}"))
    assert len(env.probe_rows) == expected
    assert sum("PROBE:" in c for c in env.contexts) == expected
    assert sum("What is your budget?" in d for d in env.drafts) == expected
    if behaviour == "probe_once":
        assert ["What is your budget?" in d for d in env.drafts[:2]] == [True, False]


@pytest.mark.parametrize("result", [
    agent.DraftResult(text="", model="m", usage={}),
    agent.DraftResult(text="Filed.", model="m", usage={}, category="fyi"),
    agent.DraftResult(text="Filed.", model="m", usage={}, category="automated"),
    agent.DraftResult(text="", model="m", usage={}, deferred=True, missing="Details"),
])
def test_pipeline_unqueued_or_empty_reply_does_not_count_probe(monkeypatch, result):
    env = _ProbePipelineEnv(monkeypatch, result=result)
    asyncio.run(pipeline.draft_for_email("t1", PROBE_EMAIL, source_message_id="m1"))
    assert env.probe_rows == []


def test_probe_logging_failure_keeps_the_draft(monkeypatch):
    env = _ProbePipelineEnv(monkeypatch)
    def fail(*a, **k):
        raise RuntimeError("probe log unavailable")
    monkeypatch.setattr(db, "crm_insert_probe_log", fail)
    assert asyncio.run(pipeline.draft_for_email("t1", PROBE_EMAIL, source_message_id="m1")) == "log-1"
    assert "PROBE:" in env.crm_context
