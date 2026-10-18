"""Frozen tenant inputs and I/O adapters. Assembly always belongs to app callers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime
import inspect
import sys
from types import SimpleNamespace

import pytest

from app import agent, db, intake, llm, pipeline, scheduling
from app import knowledge as kb
from tests.test_agent_tools import text_response, tool_response

NOW = datetime.fromisoformat("2026-09-28T09:00:00+01:00")
TODAY = NOW.date()


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return TODAY


def freeze_clock(monkeypatch, now=NOW):
    """Pin stdlib imports and already-bound app clocks, independent of import order.

    Only the test clock changes: prompt text and the hashing guard stay intact.
    The outer fixture restores modules first imported while time was frozen too.
    """
    import datetime as datetime_module

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            from datetime import timezone
            return now.astimezone(timezone.utc).replace(tzinfo=None)

    class ClockDate(date):
        @classmethod
        def today(cls):
            return now.date()

    for module in list(sys.modules.values()):
        if module and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if isinstance(value, type) and issubclass(value, datetime):
                    monkeypatch.setattr(module, name, ClockDatetime)
                elif isinstance(value, type) and issubclass(value, date):
                    monkeypatch.setattr(module, name, ClockDate)
    monkeypatch.setattr(datetime_module, "datetime", ClockDatetime)
    monkeypatch.setattr(datetime_module, "date", ClockDate)


@pytest.fixture(params=["2026-09-28", "2026-09-27", "2026-09-30"],
                ids=["monday", "sunday", "month-end"])
def ambient_today(request, monkeypatch):
    """Every real render must match the same baseline under three host dates."""
    modules_before = set(sys.modules)
    freeze_clock(monkeypatch, datetime.fromisoformat(request.param + "T11:00:00+01:00"))
    yield
    monkeypatch.undo()
    # Imports during the freeze otherwise retain a fake class after monkeypatch
    # restores stdlib. Avoid leaking a test clock into unrelated later tests.
    for name in set(sys.modules) - modules_before:
        if name.startswith("app."):
            module = sys.modules[name]
            for attr, value in list(vars(module).items()):
                if isinstance(value, type) and issubclass(value, datetime):
                    setattr(module, attr, datetime)
                elif isinstance(value, type) and issubclass(value, date):
                    setattr(module, attr, date)


BASE = {"capacity_per_window": 4, "working_days": "mon,tue,wed,thu,fri",
        "open_time": "09:00", "close_time": "17:00", "timezone": "Europe/London",
        "intake_configured_at": "2026-09-27T12:00:00Z", "booking_revision": 7,
        "windows": "slots", "slot_minutes": 60, "days_ahead": 30,
        "intake_fields": [{"label": "Contact number", "required": True}]}
SERVICE = {"name": "Service A", "minutes": 30, "price": 45.0}
GROUP = {"id": "group-a", "label": "Item", "label_plural": "Items", "cardinality": "many",
         "fields": [{"key": "name", "label": "Name", "type": "text", "required": True},
                    {"key": "valid_until", "label": "Valid until", "type": "date",
                     "required": True, "durability": "expires", "gate": True,
                     "proof": True}]}
EMAIL = {"from_address": "customer@example.invalid", "from_name": "Alex",
         "subject": "Enquiry", "body": "What information do you need?",
         "thread_id": "thread-a"}
HISTORY = [{"id": "log-previous", "kind": "inbound", "body_in": "Hello", "status": "sent", "draft_reply": "How can we help?"}]


def fixtures():
    def shape(**kw):
        return {"modules": ["scheduling"], "cfg": deepcopy(BASE), "groups": [],
                "industry": "", **kw}
    stay = {**BASE, "intake_fields": BASE["intake_fields"] + [dict(intake.STAY_FIELD)],
            "resource_types": [{"name": "Space", "count": 4}]}
    out = {
        "email_only": shape(modules=[], cfg={}),
        "gather": shape(modules=[], cfg={"intake_fields": BASE["intake_fields"]}),
        "slots": shape(),
        "services": shape(cfg={**BASE, "service_types": [{**SERVICE, "timing": "half",
            "same_day_until": "14:00", "notice_hours": 4,
            "out_of_hours": {"until": "22:00", "days": ["sun"], "price": 90,
                             "price_after": 65, "offer": "ask"}}]}),
        "exact_times": shape(cfg={**BASE, "service_types": [SERVICE]}),
        "stays_appointments": shape(cfg={**stay, "service_types": [SERVICE]}),
        "required_gate_proof": shape(cfg={**stay, "service_types": [SERVICE],
            "courses": {"hold_while_gathering": True}}, groups=[deepcopy(GROUP)]),
        "optional_gate": shape(groups=[{**GROUP, "fields": [
            {**GROUP["fields"][1], "required": False}]}]),
        "hire_delivery": shape(cfg={**stay, "delivery": {"offered": True, "fee": 20}}),
        "recurrence": shape(cfg={**BASE, "service_types": [SERVICE],
            "recurring": {"enabled": True}, "courses": {"courses": True, "waitlist": True},
            "makeups": {"on": True}, "nouns": {"booking": "session", "participant": "member"}}),
        "fsm": shape(modules=["fsm"], industry="trades"),
        "tms": shape(modules=["tms"], industry="logistics"),
        "shop_only": shape(modules=["storefront"]),
        "mixed_office_shop": shape(modules=["scheduling", "storefront"]),
        "unresolved": shape(cfg={"intake_configured_at": BASE["intake_configured_at"]}),
        "read_error": shape(read_error=True),
        "explicit_off": shape(cfg={**BASE, "capacity_per_window": 0}),
        "invalid": shape(cfg={**BASE, "timezone": "invalid/timezone"}),
        "neutral_unseen": shape(industry="precision archive support", cfg={**BASE,
            "nouns": {"booking": "allocation", "participant": "record"}}),
        # Sanitised documented rig configuration: test_out_of_hours.py CFG,
        # CALLOUT/SERVICE and the verified register. Not a fresh production read.
        "hartwell": shape(industry="trades", cfg={**BASE, "windows": "ampm",
            "open_time": "08:00", "close_time": "18:00", "capacity_per_window": 2,
            "min_notice_hours": 4, "working_days": "mon,tue,wed,thu,fri,sat",
            "service_types": [
                {"name": "[[TENANT:hw1]]Emergency callout[[/TENANT:hw1]]", "minutes": 120,
                 "price": 85.0, "timing": "half", "same_day_until": "14:00", "notice_hours": 1,
                 "out_of_hours": {"until": "22:00", "days": [6], "bank_holidays": True,
                                  "price": 140.0, "price_after": 65.0, "offer": "ask", "price_basis": "first_hour"}},
                {"name": "[[TENANT:hw2]]Boiler service[[/TENANT:hw2]]", "minutes": 60,
                 "price": 95.0, "timing": "half"}],
            "nouns": {"booking": "job"}}, business_name="Hartwell & Daughters",
            brief="[[TENANT:db.effective_config.business_brief]]Plumbing support"
                  "[[/TENANT:db.effective_config.business_brief]]"),
        "industry_pet": shape(industry="pet care"),
        "industry_equine": shape(industry="farrier"),
        "industry_clinical": shape(industry="veterinary"),
    }
    # Freeze every current non-empty runtime seed branch, in addition to the
    # unknown labels above. If a branch is added, the inventory test demands
    # a deliberate fixture/review rather than quietly leaving it unrendered.
    for industry in ("logistics", "trades", "construction", "recruitment", "wholesale", "lettings",
                     "hire", "events", "caterer", "wedding_planner", "dentist", "salon", "physio",
                     "farm_shop", "deli", "bakery", "wine_merchant", "nursery"):
        out[f"seed_{industry}"] = shape(industry=industry)
    return out


PROVENANCE = {"db.effective_config.business_brief": "Plumbing support",
              "hw1": "Emergency callout", "hw2": "Boiler service"}


def serial(value):
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(serial(v) for v in value)
    if isinstance(value, SimpleNamespace):
        return serial(vars(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class ModelIO:
    """Only the provider boundary is replaced; captures actual requests, by value."""
    def __init__(self, monkeypatch, responses=None):
        self.calls = []
        self.responses = list(responses or [text_response({"reply": "Thanks for your message.",
            "thread_state": "in_progress", "request_summary": "Enquiry",
            "action_summary": "Answered", "links": [], "action": None})])
        monkeypatch.setattr(llm, "create", self.create)
        monkeypatch.setattr(llm, "stream", self.create)
        from app import concierge, portal_chat
        from app.agents import field_service, tms
        for module in (agent, concierge, portal_chat, field_service, tms):
            monkeypatch.setattr(module, "client", lambda: None)

    async def create(self, **kwargs):
        self.calls.append(deepcopy(serial({k: kwargs[k] for k in
            ("system", "messages", "tools", "output_config", "tool_choice", "operation") if k in kwargs})))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if len(self.responses) == 1 and len(self.calls) == 1 and any(
                t.get("name") == "ready_to_write" for t in kwargs.get("tools", [])):
            response = tool_response("ready_to_write", {})
        if kwargs.get("on_delta"):
            for block in response.content:
                if block.type == "text":
                    out = kwargs["on_delta"](block.text)
                    if inspect.isawaitable(out):
                        await out
        return response


class World:
    def __init__(self, monkeypatch, spec):
        self.spec = deepcopy(spec)
        self.writes = []
        self.reads = []
        self.unexpected_io = []
        # A few persistence modules bind db() directly. Explicit empty tables
        # still let their real rendering/query code run; every other table fails.
        world = self
        class EmptyQuery:
            def __init__(self, table):
                self.table_name = table
            def __getattr__(self, name):
                if name == "execute":
                    return lambda: SimpleNamespace(data=[])
                if name not in {"select", "eq", "in_", "is_", "order", "limit", "gte", "lte"}:
                    raise AssertionError(f"Unexpected I/O operation: {self.table_name}.{name}")
                return lambda *a, **k: self
        class Store:
            def table(self, name):
                if name not in {"shop_transaction_items", "shop_transactions", "booking_notes", "waitlist", "wallet_credits"}:
                    world.unexpected_io.append(name)
                    raise AssertionError(f"Unexpected fixture table: {name}")
                return EmptyQuery(name)
        monkeypatch.setattr(db, "db", lambda: Store())
        self.cfg = {"business_brief": spec.get("brief", "Independent service business"),
                    "business_name": spec.get("business_name", "Meridian Archive"),
                    "tone": "friendly", "sign_off": "Thanks", "auto_send": False,
                    "industry": spec["industry"]}
        def read(name, result):
            def call(*a, **k):
                self.reads.append((name, a))
                return deepcopy(result)
            monkeypatch.setattr(db, name, call)
        def write(name, result=None):
            def call(*a, **k):
                self.writes.append({"name": name, "args": serial(a), "kwargs": serial(k)})
                return deepcopy(result)
            monkeypatch.setattr(db, name, call)
        for name, result in {
            "active_modules": set(spec["modules"]), "get_scheduling_config": spec["cfg"],
            "get_tenant_config": self.cfg, "get_mailbox_config": None,
            "get_tenant": {"id": "tenant-a", "name": self.cfg["business_name"],
                           "industry": spec["industry"], "mailbox": "owner@example.invalid"},
            "get_conversation": HISTORY, "bookings_for_contact": [], "list_requirement_groups": spec["groups"],
            "list_portal_messages": [], "list_documents": [], "bookings_for_log": [],
            "shop_list_products": [],
            "get_invoice_settings": {}, "crm_open_promises": [], "job_party_memberships": [],
            "list_job_parties": [], "crm_activities_for_contact": [], "crm_list_deals": [],
            "list_requirement_instances": [], "crm_list_field_definitions": [],
            "crm_contact_by_email": None, "crm_contact_by_phone": None,
            "crm_get_or_create_contact": {"id": "contact-a", "email": EMAIL["from_address"]},
            "is_ignored": False, "mailbox_self_addresses": set(),
            "own_mailbox_addresses": {"owner@example.invalid"},
            "list_vehicles": [{"id": "resource-a", "name": "Resource A", "capacity_spaces": 8, "active": True}],
            "list_engineers": [{"id": "staff-a", "name": "Staff A", "active": True}],
            "list_branches": [], "list_job_types": [],
            "get_email_log_entry": {"id": "log-a", **EMAIL, "kind": "inbound", "body_in": EMAIL["body"],
                                    "status": "pending", "channel": "email"},
        }.items():
            read(name, result)
        monkeypatch.setattr(db, "module_active", lambda t, m, **k: m in spec["modules"])
        for name in ("update_email_log_draft", "link_bookings_to_log", "set_booking_status",
                     "resolve_holds_for_log", "update_concierge_task", "release_inbound_draft",
                     "retire_superseded_drafts"):
            write(name)
        write("claim_inbound_draft", True)
        write("insert_email_log", ("log-a", True))
        # Ingest is persistence, not assembly. No tenant CRM module is active in
        # these shape fixtures; the separate pinned CRM baseline covers that leg.
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row", lambda *a, **k: {})
        async def retrieve(*a, **k):
            return []
        monkeypatch.setattr(kb, "retrieve", retrieve)
        from app import usage
        monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
        freeze_clock(monkeypatch)
        self.io = ModelIO(monkeypatch)
        if spec.get("read_error"):
            def broken(*a, **k):
                raise RuntimeError("fixture settings read unavailable")
            monkeypatch.setattr(db, "get_scheduling_config", broken)

    def assembly(self):
        return pipeline._scheduling_setup("tenant-a", EMAIL["from_address"], "thread-a", "mailbox-a",
                                          email=EMAIL, history=HISTORY, existing_bookings=[])

    def run(self, channel):
        if channel == "draft":
            return asyncio.run(pipeline.draft_for_email("tenant-a", deepcopy(EMAIL), mailbox_id="mailbox-a"))
        return asyncio.run(pipeline.redraft_email_log("tenant-a", "log-a"))
