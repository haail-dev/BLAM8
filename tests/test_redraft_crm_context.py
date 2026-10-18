"""A redraft goes in knowing everything the first draft knew.

Found live on the Little Paws rig, 2026-09-21, while filming: the first draft
correctly read "Known dependants ... Bella ... Vaccinations: 2026-10-03", but
`redraft_email_log` never built a crm_context at all and passed none to
`agent.draft_reply`. So the thumbs-down — which the product presents to the
owner as "try again, better" — wrote with strictly LESS knowledge than the
attempt it replaced: re-asking for details already on record, blind to the
customer's dependants, and unable to reason about the expiry that gates the
booking.

Run: python -m pytest tests/test_redraft_crm_context.py -q
"""
import asyncio
from types import SimpleNamespace

from app import agent, pipeline

# The rig's shape: a Dog group whose vaccination expiry is a hard gate.
GROUPS = [{"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "cardinality": "many",
           "fields": [{"key": "breed", "label": "Breed", "type": "text"},
                      {"key": "vaccinations", "label": "Vaccinations", "type": "date",
                       "durability": "expires", "gate": True}]}]
INSTANCES = [{"id": "i-bella", "group_id": "g-dog", "group_label": "Dog", "name": "Bella",
              "data": {"breed": "Cockapoo", "vaccinations": "2026-10-03"}}]

CONTACT = {"id": "c1", "tenant_id": "t1", "name": "Hannah Reed",
           "email": "hannah@example.com", "phone": "07700900123", "fields": {}}


def _async(value):
    async def f(*a, **k):
        return value
    return f


def _env(monkeypatch, *, groups=GROUPS, instances=INSTANCES, modules=("crm",)):
    """A redraft of a pending row for a contact who HAS dependants on record.
    Everything the drafter would otherwise read is stubbed empty, so what the
    assertions see comes from the dependants block alone."""
    seen = {}
    entry = {"id": "log1", "from_address": "hannah@example.com", "subject": "Grooming?",
             "kind": "inbound", "body_in": "Can I book Bella in?", "status": "pending",
             "mailbox_id": None, "thread_id": "t", "channel": "email"}

    monkeypatch.setattr(pipeline.db, "get_email_log_entry", lambda log_id, t, client=None: entry)
    monkeypatch.setattr(pipeline.db, "effective_config", lambda t, m, client=None: {})
    monkeypatch.setattr(pipeline.db, "get_tenant", lambda t, client=None: {"industry": "pet care"})
    monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.db, "active_modules", lambda t, client=None: set())
    monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.db, "module_active",
                        lambda t, m, client=None: m in modules)
    # The CRM legs the context is built from.
    monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda t, e, client=None: CONTACT)
    monkeypatch.setattr(pipeline.db, "crm_contact_by_phone", lambda t, p, client=None: CONTACT)
    monkeypatch.setattr(pipeline.db, "crm_activities_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.db, "crm_list_field_definitions", lambda t, **k: [])
    monkeypatch.setattr(pipeline.db, "crm_list_deals", lambda t, **k: [])
    monkeypatch.setattr(pipeline.db, "crm_open_promises", lambda t, **k: [])
    monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda t, client=None: {})
    monkeypatch.setattr(pipeline.db, "list_requirement_groups", lambda t, client=None: groups)
    monkeypatch.setattr(pipeline.db, "list_requirement_instances",
                        lambda t, cid, client=None: instances)
    # The other context blocks: quiet, so the assertions read the dependants only.
    from app import courses, customer_log, makeups, wallet
    monkeypatch.setattr(wallet, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(makeups, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(customer_log, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(courses, "context_block", lambda *a, **k: "")

    monkeypatch.setattr(pipeline, "_merge_portal_history", lambda t, f, h, client=None: h)
    monkeypatch.setattr(pipeline, "_party_context", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_documents_context", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_merge_handoff_usage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_unbacked_refs", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_draft_sources", lambda r: [])
    monkeypatch.setattr(pipeline, "_scheduling_setup",
                        lambda *a, **k: (None, None, None, "", {"scheduling": False,
                                                                "transport": False}))
    monkeypatch.setattr(pipeline.kb, "retrieve", _async([]))
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda r: "")
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "strip_quoted", lambda b: b)
    monkeypatch.setattr(pipeline.textfmt, "to_plain_text", lambda t: t)

    async def draft_reply(cfg, email, **kw):
        seen["crm_context"] = kw.get("crm_context")
        return SimpleNamespace(text="reworded", missing=None, deferred=False, blocked=None,
                               thread_state="in_progress", request_summary="", action_summary="",
                               contact_name=None, contact_company=None, usage=None,
                               searched_for=[], booking_details=[], category_reason=None,
                               reasoning=None, request_id=None, category="draft_ready",
                               sentiment=None, promises=[], extracted=None, parties=[],
                               dependants=None, usage_meta=None, model="m",
                               scheduling_requested=False, scheduling_gap=False, held=False,
                               offered=False, cancel_requested=False, reschedule_requested=False,
                               stock=None, transport=None)

    monkeypatch.setattr(pipeline.agent, "draft_reply", draft_reply)
    return seen


def test_a_redraft_carries_the_dependants_into_the_prompt(monkeypatch):
    seen = _env(monkeypatch)
    assert asyncio.run(pipeline.redraft_email_log("t1", "log1")) == "reworded"
    ctx = seen["crm_context"]
    assert "Known dependants" in ctx
    assert "Bella" in ctx and "Cockapoo" in ctx
    assert "Vaccinations: 2026-10-03" in ctx        # the date the booking gate reads
    # And it reaches the model: the block lands inside <customer_context>.
    prompt = agent.build_system_prompt({}, crm_context=ctx)
    block = prompt.split("<customer_context>")[1].split("</customer_context>")[0]
    assert "Bella" in block and "Vaccinations: 2026-10-03" in block


def test_a_failing_sub_block_still_leaves_the_dependants(monkeypatch):
    """Best-effort per block: the wallet blowing up must not cost the redraft
    the rest of what it knows."""
    seen = _env(monkeypatch)
    from app import wallet

    def boom(*a, **k):
        raise RuntimeError("wallet down")
    monkeypatch.setattr(wallet, "context_block", boom)
    assert asyncio.run(pipeline.redraft_email_log("t1", "log1")) == "reworded"
    assert "Bella" in seen["crm_context"]


def test_no_dependants_on_record_means_no_block(monkeypatch):
    seen = _env(monkeypatch, instances=[])
    asyncio.run(pipeline.redraft_email_log("t1", "log1"))
    assert "Known dependants" not in (seen["crm_context"] or "")


def test_an_email_tier_tenant_redrafts_with_no_crm_context(monkeypatch):
    """The CRM-tier gate is the same on both paths: no CRM module, no context."""
    seen = _env(monkeypatch, modules=())
    asyncio.run(pipeline.redraft_email_log("t1", "log1"))
    assert seen["crm_context"] == ""
