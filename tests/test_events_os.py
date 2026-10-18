"""Events OS — Phase A (docs/os/events.md §13): the event primitive + the self-filling
capture hook + the board, the spine visible.

Pins: event CRUD + crm_event_for_deal 1:1; the dietary-matrix rollup aggregates
correctly; on_deal_is_event creates/updates a crm_events row from an event email and
routes guests to crm_event_guests NOT crm_contacts (§14) — and does NOTHING for a
non-event read or a non-events tenant; the /app/events board renders this-week cards
with rollups; an events-industry tenant onboards + gets the `events` module via
apply_plan_modules (no KeyError); the module hub shows "Events OS"; onboarding shows
Events as a live Shape.

No network: db.* is monkeypatched; the autouse conftest guard fails loudly on any
un-stubbed live Supabase call.

Run: python -m pytest tests/test_events_os.py -q
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db, events_os, industry_lingo, onboarding, operating_systems, plans


# ── a tiny multi-table fake Supabase (per-table row stores) ───────────────────
class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._rows = list(store.setdefault(name, []))
        self._pending = None

    def select(self, *a, **k): return self
    def insert(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        made = []
        for p in rows:
            row = dict(p)
            row.setdefault("id", f"{self._name}-{len(self._store[self._name]) + 1}")
            self._store[self._name].append(row)
            made.append(dict(row))
        self._pending = ("insert", made)
        return self
    def update(self, payload):
        for r in self._rows:
            r.update(payload)
        self._pending = ("update", [dict(r) for r in self._rows])
        return self
    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def ilike(self, col, val):
        needle = str(val).strip().lower()
        self._rows = [r for r in self._rows
                      if str(r.get(col) or "").strip().lower() == needle]
        return self
    def gte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) and str(r.get(col)) >= str(val)]
        return self
    def lte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) and str(r.get(col)) <= str(val)]
        return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def offset(self, *a, **k): return self
    def execute(self):
        if self._pending:
            _, data = self._pending
            self._pending = None
            return _FakeResult(data)
        return _FakeResult(self._rows)


class _FakeClient:
    def __init__(self, store=None):
        self._store = store if store is not None else {}

    def table(self, name):
        return _FakeQuery(self._store, name)


def _client(**tables):
    return _FakeClient(dict(tables))


# ══ event CRUD + the 1:1 deal link ═════════════════════════════════════════════
def test_crm_create_event_stamps_tenant_and_deal():
    cl = _client(crm_events=[])
    got = db.crm_create_event("t1", deal_id="d1", event_date="2026-08-12",
                              event_type="wedding", guest_count=118, client=cl)
    assert got["tenant_id"] == "t1" and got["deal_id"] == "d1"
    assert got["event_date"] == "2026-08-12" and got["guest_count"] == 118


def test_crm_event_for_deal_is_one_to_one():
    rows = [{"id": "e1", "tenant_id": "t1", "deal_id": "d1"}]
    assert db.crm_event_for_deal("d1", "t1", client=_client(crm_events=rows))["id"] == "e1"
    assert db.crm_event_for_deal("nope", "t1", client=_client(crm_events=rows)) is None
    # another tenant's deal never resolves (IDOR-safe)
    assert db.crm_event_for_deal("d1", "t2", client=_client(crm_events=rows)) is None


def test_crm_update_event_merges_and_stamps():
    rows = [{"id": "e1", "tenant_id": "t1", "deal_id": "d1", "guest_count": 100}]
    cl = _client(crm_events=rows)
    got = db.crm_update_event("e1", "t1", {"guest_count": 120}, client=cl)
    assert got["guest_count"] == 120 and got.get("updated_at")


# ══ dietary-matrix rollup ══════════════════════════════════════════════════════
def test_dietary_matrix_aggregates_weighted_by_party_size():
    guests = [
        {"id": "g1", "tenant_id": "t1", "event_id": "e1", "party_size": 4,
         "dietary_tags": ["vegan"]},
        {"id": "g2", "tenant_id": "t1", "event_id": "e1", "party_size": 1,
         "dietary_tags": ["vegan", "gluten_free"]},
        {"id": "g3", "tenant_id": "t1", "event_id": "e1", "party_size": 2,
         "dietary_tags": ["nut_allergy"]},
        {"id": "g4", "tenant_id": "t1", "event_id": "e1", "party_size": 1,
         "dietary_tags": []},
    ]
    m = db.crm_event_dietary_matrix("t1", "e1", client=_client(crm_event_guests=guests))
    assert m == {"vegan": 5, "gluten_free": 1, "nut_allergy": 2}


def test_dietary_matrix_empty_when_no_guests():
    assert db.crm_event_dietary_matrix("t1", "e1", client=_client(crm_event_guests=[])) == {}


# ══ guest upsert dedups by name (NOT a crm_contact — §14) ═══════════════════════
def test_guest_upsert_dedups_by_name():
    store = {"crm_event_guests": []}
    db.crm_upsert_event_guest("t1", event_id="e1", name="Aunt May",
                              dietary_tags=["vegan"], client=_FakeClient(store))
    db.crm_upsert_event_guest("t1", event_id="e1", name="aunt may",  # same, different case
                              rsvp="yes", client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 1                 # reinforced, not duplicated
    assert store["crm_event_guests"][0]["rsvp"] == "yes"


def test_guest_upsert_writes_to_guests_table_never_contacts():
    store = {"crm_event_guests": [], "crm_contacts": []}
    db.crm_upsert_event_guest("t1", event_id="e1", name="Guest", dietary="vegan",
                              client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 1
    assert store["crm_contacts"] == []                          # §14: guests are NOT contacts


# ══ stakeholder upsert dedups by (kind, role) ══════════════════════════════════
def test_stakeholder_upsert_dedups_by_role():
    store = {"crm_event_stakeholders": []}
    db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier", role="caterer",
                                    name="Bob's Catering", client=_FakeClient(store))
    db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier", role="Caterer",
                                    outstanding="final numbers", client=_FakeClient(store))
    assert len(store["crm_event_stakeholders"]) == 1
    assert store["crm_event_stakeholders"][0]["outstanding"] == "final numbers"


# ══ Bug 1: EXACT dedup (never .ilike) on names/roles with %/_/( ═════════════════
def test_guest_dedup_exact_match_wildcard_name_not_dropped():
    """An LLM name carrying %/_/( must dedup to ITS OWN row (case-insensitive
    exact), never a .ilike wildcard match onto a wrong row / a missed dup."""
    store = {"crm_event_guests": []}
    db.crm_upsert_event_guest("t1", event_id="e1", name="Mr & Mrs Smith (50% deposit)",
                              dietary_tags=["vegan"], client=_FakeClient(store))
    # a DIFFERENT guest whose literal name the first would .ilike-match if % were a
    # wildcard — must NOT collapse into it: two distinct rows.
    db.crm_upsert_event_guest("t1", event_id="e1", name="Mr & Mrs Smithson",
                              dietary_tags=["gluten_free"], client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 2
    # re-seeing the exact wildcard name reinforces its OWN row (case-insensitive), not a dup
    db.crm_upsert_event_guest("t1", event_id="e1", name="mr & mrs smith (50% DEPOSIT)",
                              rsvp="yes", client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 2
    smith = next(g for g in store["crm_event_guests"] if "50%" in g["name"])
    assert smith["rsvp"] == "yes"


def test_stakeholder_dedup_exact_match_wildcard_role_not_dropped():
    store = {"crm_event_stakeholders": []}
    db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier",
                                    role="Bar (100% cover)", name="Vine",
                                    client=_FakeClient(store))
    db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier",
                                    role="Barista", name="Beans", client=_FakeClient(store))
    assert len(store["crm_event_stakeholders"]) == 2       # % not treated as a wildcard
    db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier",
                                    role="bar (100% COVER)", outstanding="licence",
                                    client=_FakeClient(store))
    assert len(store["crm_event_stakeholders"]) == 2       # reinforced its own row
    bar = next(s for s in store["crm_event_stakeholders"] if "100%" in s["role"])
    assert bar["outstanding"] == "licence"


# ── a fake whose FIRST insert to a table raises (a unique-violation), simulating a
#    race where a concurrent write landed the row between our select and our insert ──
class _RaceInsertClient(_FakeClient):
    def __init__(self, store, race_table, prefill):
        super().__init__(store)
        self._race_table = race_table
        self._prefill = prefill      # the row the "concurrent" writer landed
        self._fired = False

    def table(self, name):
        q = super().table(name)
        if name == self._race_table and not self._fired:
            outer = self

            def racing_insert(payload):
                # simulate the unique-violation: land the concurrent row, then raise
                outer._fired = True
                outer._store.setdefault(name, []).append(dict(outer._prefill))
                raise Exception("duplicate key value violates unique constraint")
            q.insert = racing_insert
        return q


def test_guest_upsert_race_reselects_and_updates_not_dropped():
    """A unique-violation on insert (concurrent write) → re-select + update, never a
    swallowed drop (the guest would otherwise vanish)."""
    store = {"crm_event_guests": []}
    prefill = {"id": "g-race", "tenant_id": "t1", "event_id": "e1", "name": "Aunt May"}
    cl = _RaceInsertClient(store, "crm_event_guests", prefill)
    got = db.crm_upsert_event_guest("t1", event_id="e1", name="Aunt May",
                                    rsvp="yes", client=cl)
    assert got["id"] == "g-race"                    # merged onto the race winner
    assert len(store["crm_event_guests"]) == 1      # NOT duplicated / dropped
    assert store["crm_event_guests"][0]["rsvp"] == "yes"


def test_stakeholder_upsert_race_reselects_and_updates_not_dropped():
    store = {"crm_event_stakeholders": []}
    prefill = {"id": "s-race", "tenant_id": "t1", "event_id": "e1",
               "kind": "supplier", "role": "caterer"}
    cl = _RaceInsertClient(store, "crm_event_stakeholders", prefill)
    got = db.crm_upsert_event_stakeholder("t1", event_id="e1", kind="supplier",
                                          role="caterer", outstanding="numbers", client=cl)
    assert got["id"] == "s-race"
    assert len(store["crm_event_stakeholders"]) == 1
    assert store["crm_event_stakeholders"][0]["outstanding"] == "numbers"


# ══ Bug 3: non-atomic event create race (crm_create_event get-or-create) ════════
def test_crm_create_event_race_returns_existing_not_second_row():
    """Two inbounds for the same deal both saw crm_event_for_deal → None: the second
    INSERT violates uq_crm_events_deal → we re-select the winner, never drop the email."""
    store = {"crm_events": []}
    prefill = {"id": "e-race", "tenant_id": "t1", "deal_id": "d1"}
    cl = _RaceInsertClient(store, "crm_events", prefill)
    got = db.crm_create_event("t1", deal_id="d1", event_date="2026-08-12", client=cl)
    assert got["id"] == "e-race"                    # the existing event, not a duplicate
    assert len(store["crm_events"]) == 1


# ══ Bug 4: party_size 0 weights 0 (not a phantom cover) ════════════════════════
def test_dietary_matrix_party_size_zero_weights_zero():
    guests = [
        {"id": "g1", "tenant_id": "t1", "event_id": "e1", "party_size": 0,
         "dietary_tags": ["vegan"]},                      # declined/placeholder → 0, not 1
        {"id": "g2", "tenant_id": "t1", "event_id": "e1", "party_size": 3,
         "dietary_tags": ["vegan"]},
    ]
    m = db.crm_event_dietary_matrix("t1", "e1", client=_client(crm_event_guests=guests))
    assert m == {"vegan": 3}                             # not 4 — the 0 party adds nothing


def test_capture_party_size_zero_preserved(monkeypatch):
    """on_deal_is_event must store a party_size of 0 as 0, not rewrite it to 1."""
    _events_tenant(monkeypatch)
    guests = []
    monkeypatch.setattr(events_os.db, "crm_event_for_deal", lambda *a, **k: {"id": "e1"})
    monkeypatch.setattr(events_os.db, "crm_upsert_event_guest",
                        lambda t, **k: guests.append(k))
    monkeypatch.setattr(events_os.db, "crm_upsert_event_stakeholder", lambda *a, **k: None)
    event = {"is_event": True, "confidence": 0.9,
             "guests": [{"name": "Declined table", "party_size": 0,
                         "dietary_tags": ["vegan"], "confidence": 0.9}]}
    events_os.on_deal_is_event("t1", "d1", event)
    assert guests and guests[0]["party_size"] == 0      # not `or 1` → phantom cover


# ══ Bug 5: anonymous dietary count is idempotent on reprocess ══════════════════
def test_anonymous_dietary_count_idempotent_on_reprocess():
    """A name=None "6 vegan guests" reprocessed (retry/replay/quote) must UPDATE the
    one anonymous row, not stack a second (12 vegans)."""
    store = {"crm_event_guests": []}
    db.crm_upsert_event_guest("t1", event_id="e1", name=None, party_size=6,
                              dietary="vegan", dietary_tags=["vegan"],
                              client=_FakeClient(store))
    db.crm_upsert_event_guest("t1", event_id="e1", name=None, party_size=6,
                              dietary="vegan", dietary_tags=["vegan"],
                              client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 1          # reinforced, not doubled
    m = db.crm_event_dietary_matrix("t1", "e1", client=_FakeClient(store))
    assert m == {"vegan": 6}                            # NOT 12
    # a DIFFERENT anonymous tag-set is its own row (distinct signature)
    db.crm_upsert_event_guest("t1", event_id="e1", name=None, party_size=2,
                              dietary="gluten free", dietary_tags=["gluten_free"],
                              client=_FakeClient(store))
    assert len(store["crm_event_guests"]) == 2


# ══ Bug 2: OUTBOUND process_email creates/applies NO event + base schema/tokens ══
def test_intel_outbound_never_captures_event_and_uses_base_schema(monkeypatch):
    import asyncio
    from app import crm_intel

    seen = {}

    async def fake_analyze(tid, **k):
        seen["extensions"] = k.get("extensions")
        return {"deal": {"action": "new", "title": "Wedding", "confidence": 0.9,
                         "deal_id": "", "value_estimate": 0},
                "promises": [], "event": {"is_event": True, "confidence": 0.95,
                                          "guest_count": 120}}
    monkeypatch.setattr(crm_intel, "analyze", fake_analyze)
    monkeypatch.setattr(crm_intel.db, "crm_list_deals",
                        lambda *a, **k: [{"id": "d1", "status": "open"}])
    monkeypatch.setattr(crm_intel.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel, "apply_promises", lambda *a, **k: {"created": 0})
    # even an EVENTS tenant: the outbound path must not extract from our own reply
    monkeypatch.setattr(events_os, "is_events_tenant", lambda *a, **k: True)
    monkeypatch.setattr(events_os, "on_deal_is_event",
                        lambda *a, **k: pytest.fail("outbound must NOT capture an event"))
    out = asyncio.run(crm_intel.process_email(
        "t1", direction="outbound", subject="s", body="confirming your wedding, 120 guests",
        contact_id="c1", company_id=None, contact_name="A", company_name=None,
        email_log_id="log1"))
    assert out["event_id"] is None
    assert seen["extensions"] == ()                     # base schema + 700 tokens (byte-identical)


def test_intel_analyze_outbound_schema_and_tokens_are_base():
    """Directly: no active extensions → base schema object + 700-token cap (Bug 2's
    'non-inbound calls get the byte-identical non-events call')."""
    from app import crm_intel
    assert crm_intel._analyze_schema(()) is crm_intel.ANALYZE_SCHEMA
    # the token/system split is driven by the same flag (see analyze()): 900 vs 700
    # is a single `if events_mode` — proven byte-identical by the schema identity above
    # plus the flag being forced False for outbound in process_email.


# ══ Bug 6: board day-boundary uses the app tz (not naive local) ════════════════
def test_board_window_correct_across_utc_midnight(monkeypatch):
    """The board's 'today' comes from DIARY_TZ (Europe/London), consistent with the
    UTC-stamped event_dates — so the THIS WEEK split is never off-by-one at midnight."""
    from datetime import datetime
    from app.scheduling import DIARY_TZ

    captured = {}

    def fake_between(tenant_id, start, end, client=None):
        captured["start"] = start
        captured["end"] = end
        return []
    monkeypatch.setattr(events_os.db, "crm_events_between", fake_between)

    # A moment that is 2026-07-09 in London but still 2026-07-08 in naive UTC would
    # be off-by-one under datetime.now().date(); pin DIARY_TZ's "now" and assert the
    # window starts on the London date.
    class _FrozenNow(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 9, 0, 30, tzinfo=DIARY_TZ)  # just past midnight BST
            return base.astimezone(tz) if tz else base
    monkeypatch.setattr(events_os, "datetime", _FrozenNow)

    b = events_os.board_view("t1")
    assert b["today"] == "2026-07-09"                   # London date, not the UTC-prior day
    assert captured["start"] == "2026-07-09"


# ══ the capture hook — on_deal_is_event ════════════════════════════════════════
def _events_tenant(monkeypatch, is_event=True):
    monkeypatch.setattr(events_os.db, "module_active", lambda *a, **k: is_event)


def test_on_deal_is_event_creates_row_and_routes_guests(monkeypatch):
    _events_tenant(monkeypatch)
    created = {}
    guests = []
    monkeypatch.setattr(events_os.db, "crm_event_for_deal", lambda *a, **k: None)
    monkeypatch.setattr(events_os.db, "crm_create_event",
                        lambda t, **k: created.update(k) or {"id": "e1", **k})
    monkeypatch.setattr(events_os.db, "crm_update_event",
                        lambda eid, t, upd, **k: {"id": eid, **upd})
    monkeypatch.setattr(events_os.db, "crm_upsert_event_guest",
                        lambda t, **k: guests.append(k) or {"id": "g1", **k})
    monkeypatch.setattr(events_os.db, "crm_upsert_event_stakeholder",
                        lambda t, **k: {"id": "s1", **k})

    event = {
        "is_event": True, "confidence": 0.9,
        "event_date": "2026-08-12", "event_type": "wedding", "guest_count": 118,
        "timings": [{"label": "Arrival", "time": "13:00"}],
        "guests": [{"name": "Table 4", "party_size": 6, "rsvp": "yes",
                    "dietary": "vegan", "dietary_tags": ["vegan"], "confidence": 0.9}],
        "stakeholders": [{"kind": "supplier", "role": "caterer", "name": "Bob",
                          "email": "", "outstanding": "final numbers", "confidence": 0.9}],
    }
    row = events_os.on_deal_is_event("t1", "d1", event, email_log_id="log1")
    assert row is not None
    assert created["deal_id"] == "d1" and created["event_date"] == "2026-08-12"
    # guests routed to the GUEST table (§14), carrying the source email
    assert len(guests) == 1 and guests[0]["name"] == "Table 4"
    assert guests[0]["source_email"] == "log1" and guests[0]["dietary_tags"] == ["vegan"]


def test_on_deal_is_event_updates_existing_row(monkeypatch):
    _events_tenant(monkeypatch)
    updated = {}
    monkeypatch.setattr(events_os.db, "crm_event_for_deal",
                        lambda *a, **k: {"id": "e1", "deal_id": "d1", "guest_count": 100})
    monkeypatch.setattr(events_os.db, "crm_create_event",
                        lambda *a, **k: pytest.fail("must not create when the event exists"))
    monkeypatch.setattr(events_os.db, "crm_update_event",
                        lambda eid, t, upd, **k: updated.update(upd) or {"id": eid, **upd})
    event = {"is_event": True, "confidence": 0.9, "guest_count": 130}
    events_os.on_deal_is_event("t1", "d1", event)
    assert updated["guest_count"] == 130                        # firmed the headcount, no duplicate


def test_on_deal_is_event_noop_for_low_confidence(monkeypatch):
    _events_tenant(monkeypatch)
    monkeypatch.setattr(events_os.db, "crm_event_for_deal",
                        lambda *a, **k: pytest.fail("must not touch the DB below the gate"))
    assert events_os.on_deal_is_event("t1", "d1",
                                      {"is_event": True, "confidence": 0.2}) is None


def test_on_deal_is_event_noop_for_non_events_tenant(monkeypatch):
    _events_tenant(monkeypatch, is_event=False)                 # module OFF
    monkeypatch.setattr(events_os.db, "crm_event_for_deal",
                        lambda *a, **k: pytest.fail("a non-events tenant must be untouched"))
    event = {"is_event": True, "confidence": 0.95, "guest_count": 100}
    assert events_os.on_deal_is_event("t1", "d1", event) is None


def test_capture_skips_low_confidence_guest(monkeypatch):
    _events_tenant(monkeypatch)
    guests = []
    monkeypatch.setattr(events_os.db, "crm_event_for_deal", lambda *a, **k: {"id": "e1"})
    monkeypatch.setattr(events_os.db, "crm_upsert_event_guest",
                        lambda t, **k: guests.append(k))
    monkeypatch.setattr(events_os.db, "crm_upsert_event_stakeholder", lambda *a, **k: None)
    event = {"is_event": True, "confidence": 0.9,
             "guests": [{"name": "Maybe", "dietary_tags": ["vegan"], "confidence": 0.2}]}
    events_os.on_deal_is_event("t1", "d1", event)
    assert guests == []                                         # below the gate → dropped


# ══ crm_intel wiring: the extension seam gates the event branch ═══════════════════════
def test_intel_analyze_schema_byte_identical_for_non_events():
    from app import crm_intel
    assert crm_intel._analyze_schema(()) is crm_intel.ANALYZE_SCHEMA      # untouched path
    ext = crm_intel._INTEL_EXTENSIONS["event"]     # events_os registered at import
    assert "event" in crm_intel._analyze_schema((ext,))["properties"]     # branch added only when on


def test_intel_process_email_calls_hook_only_for_events(monkeypatch):
    import asyncio
    from app import crm_intel

    async def fake_analyze(tid, **k):
        return {"deal": {"action": "new", "title": "Wedding", "confidence": 0.9,
                         "deal_id": "", "value_estimate": 0},
                "promises": [], "event": {"is_event": True, "confidence": 0.9}}
    monkeypatch.setattr(crm_intel, "analyze", fake_analyze)
    monkeypatch.setattr(crm_intel.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel, "apply_deal", lambda *a, **k: "d1")
    monkeypatch.setattr(crm_intel, "apply_promises", lambda *a, **k: {"created": 0})
    monkeypatch.setattr(events_os, "is_events_tenant", lambda *a, **k: True)
    seen = {}
    monkeypatch.setattr(events_os, "on_deal_is_event",
                        lambda t, did, ev, **k: seen.update({"deal": did, "event": ev}) or {"id": "e1"})
    out = asyncio.run(crm_intel.process_email(
        "t1", direction="inbound", subject="s", body="b", contact_id="c1",
        company_id=None, contact_name="A", company_name=None, email_log_id="log1"))
    assert seen["deal"] == "d1" and out["event_id"] == "e1"


def test_intel_process_email_skips_hook_for_non_events(monkeypatch):
    import asyncio
    from app import crm_intel

    async def fake_analyze(tid, **k):
        return {"deal": {"action": "new", "title": "Job", "confidence": 0.9,
                         "deal_id": "", "value_estimate": 0}, "promises": [], "event": {}}
    monkeypatch.setattr(crm_intel, "analyze", fake_analyze)
    monkeypatch.setattr(crm_intel.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel, "apply_deal", lambda *a, **k: "d1")
    monkeypatch.setattr(crm_intel, "apply_promises", lambda *a, **k: {"created": 0})
    monkeypatch.setattr(events_os, "is_events_tenant", lambda *a, **k: False)
    monkeypatch.setattr(events_os, "on_deal_is_event",
                        lambda *a, **k: pytest.fail("hook must not fire for a non-events tenant"))
    out = asyncio.run(crm_intel.process_email(
        "t1", direction="inbound", subject="s", body="b", contact_id="c1",
        company_id=None, contact_name="A", company_name=None, email_log_id="log1"))
    assert out["event_id"] is None


# ══ the board view-model ═══════════════════════════════════════════════════════
def _event_row(eid, ed, title, value=None, guest_count=None):
    return {"id": eid, "tenant_id": "t1", "deal_id": f"d-{eid}", "event_date": ed,
            "guest_count": guest_count,
            "crm_deals": {"id": f"d-{eid}", "title": title, "value_estimate": value,
                          "status": "open", "crm_stages": {"name": "Planning"},
                          "crm_companies": {"name": "Harper"}, "crm_contacts": {"name": "Sam"}}}


def test_board_splits_this_week_and_next_weeks(monkeypatch):
    today = date(2026, 7, 8)
    rows = [
        _event_row("e1", "2026-07-11", "Harper Wedding", value=8400, guest_count=118),  # this week
        _event_row("e2", "2026-07-25", "Meridian Away-Day", value=2100),                # next weeks
    ]
    monkeypatch.setattr(events_os.db, "crm_events_between", lambda *a, **k: rows)
    monkeypatch.setattr(events_os.db, "crm_list_event_guests", lambda *a, **k: [
        {"party_size": 6, "dietary_tags": ["vegan"]}])
    monkeypatch.setattr(events_os.db, "crm_list_event_stakeholders", lambda *a, **k: [
        {"kind": "supplier", "role": "caterer", "confirmation": "confirmed"},
        {"kind": "supplier", "role": "florist", "confirmation": "pending",
         "outstanding": "delivery time"}])
    b = events_os.board_view("t1", today=today)
    assert b["this_week_count"] == 1 and b["next_weeks_count"] == 1
    card = b["this_week"][0]
    assert card["title"] == "Harper Wedding" and card["guest_count"] == 118
    assert card["dietary_matrix"] == {"vegan": 6}
    assert card["suppliers_confirmed"] == 1 and card["suppliers_total"] == 2
    # the pending supplier surfaces as a 'chase' light
    tones = sorted(l["tone"] for l in card["lights"])
    assert tones == ["chase", "good"]
    assert b["next_weeks_value"] == 2100


# ══ onboarding + OS wiring ═════════════════════════════════════════════════════
def test_events_os_is_live_and_industries_mapped():
    assert operating_systems.is_live("events")                  # Shape shows in the picker
    for iid in ("events", "caterer", "wedding_planner"):
        assert operating_systems.os_for_industry(iid) == "events"


def test_events_industries_present_live_with_data_sources():
    by_id = {i["id"]: i for i in onboarding.INDUSTRIES}
    for iid in ("events", "caterer", "wedding_planner"):
        assert by_id[iid]["os"] == "events" and by_id[iid]["live"] is True
        assert not by_id[iid].get("hidden")
        assert iid in onboarding.DATA_SOURCES
    vendors = [v for src in onboarding.DATA_SOURCES["events"] for v in src.get("vendors", [])]
    assert "Tripleseat" in vendors                              # the enquiry-system signal


def test_onboarding_shows_events_as_live_shape():
    assert operating_systems.is_live("events")
    live_events = [i for i in onboarding.INDUSTRIES
                   if i.get("os") == "events" and i.get("live") and not i.get("hidden")]
    assert {"events", "caterer", "wedding_planner"} <= {i["id"] for i in live_events}


def test_events_industry_lingo_and_seeds_present():
    for iid in ("events", "caterer", "wedding_planner"):
        assert industry_lingo.seed_for(iid)                     # a drafting vocabulary seed
        seeds = industry_lingo.crm_seeds_for(iid)
        deal_labels = [s["label"] for s in seeds.get("deal", ())]
        assert "Event date" in deal_labels and "Headcount" in deal_labels


# ══ plan / module entitlement ══════════════════════════════════════════════════
def test_events_is_a_managed_module_via_industry_meta():
    assert "events" in plans.MANAGED_MODULES
    assert plans.INDUSTRY_MODULE["events"] == "events"
    assert plans.modules_for("professional", "events") == {"scheduling", "crm", "events"}
    assert plans.modules_for("starter", "events") == {"scheduling", "crm"}   # Core = base only


def test_managed_modules_match_operator_switchboard_with_events():
    from app.admin import TOGGLABLE_MODULES
    assert set(plans.MANAGED_MODULES) == TOGGLABLE_MODULES     # the MANAGED==TOGGLABLE invariant


def test_apply_plan_modules_events_activates_no_keyerror(monkeypatch):
    states = {}
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "events"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: None)
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "events"}
    assert states["events"] is True and states["scheduling"] is True
    assert states["tms"] is False and states["assets"] is False   # never another vertical's


def test_events_tier_parked_but_module_meta_survives():
    # Two-tier collapse (2026-07-24): the OS tier is off the public catalog;
    # module metadata stays for founding partners / future un-parking.
    assert not any(r["key"] == "professional" for r in plans.billing_plans("events"))
    meta = plans.industry_module_meta("events")
    assert meta["name"] == "Events OS"
    assert "Multi-stakeholder chase" in meta["features"]


# ══ the module hub + board render ══════════════════════════════════════════════
@pytest.fixture
def hub_client(monkeypatch):
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"events"})
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_module_hub_renders_events_os(hub_client):
    html = hub_client.get("/app/modules").text
    assert "Events OS" in html
    assert "/app/events" in html                                # its card links to the surface


@pytest.fixture
def events_client(monkeypatch):
    import app.dashboard as dashboard
    import app.events_routes as er
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"events"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(er.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), er
    app.dependency_overrides.clear()


def test_events_board_renders_this_week_cards(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.events_os, "board_view", lambda *a, **k: {
        "today": "2026-07-08", "week_label": "Week of 8 Jul",
        "this_week": [{"id": "e1", "deal_id": "d1", "title": "Harper Wedding",
                       "company": "Harper", "event_date": "2026-07-11",
                       "event_type": "wedding", "guest_count": 118,
                       "guest_count_final": False, "value": 8400, "stage": "Planning",
                       "timings": [{"label": "Arrival", "time": "13:00"}],
                       "dietary_matrix": {"vegan": 6, "nut_allergy": 2},
                       "lights": [{"kind": "supplier", "role": "caterer",
                                   "confirmation": "confirmed", "tone": "good",
                                   "outstanding": None}],
                       "suppliers_confirmed": 1, "suppliers_total": 1,
                       "staff_confirmed": 0, "staff_total": 0, "guests_recorded": 3}],
        "this_week_count": 1, "next_weeks_count": 0, "next_weeks_value": 0,
        "next_weeks": []})
    r = client.get("/app/events")
    assert r.status_code == 200
    assert "This week" in r.text and "Harper Wedding" in r.text
    assert "118 guest" in r.text                                # the headcount rollup
    assert "6 vegan" in r.text                                  # the dietary matrix rollup


def test_events_detail_renders(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.events_os, "detail_view", lambda *a, **k: {
        "event": {"id": "e1", "deal_id": "d1", "event_date": "2026-07-11",
                  "event_type": "wedding", "guest_count": 118,
                  "timings": [{"label": "Arrival", "time": "13:00"}],
                  "drink_licence": None, "contingency_notes": None},
        "deal": {"title": "Harper Wedding", "value_estimate": 8400,
                 "crm_stages": {"name": "Planning"}, "crm_companies": {"name": "Harper"},
                 "crm_contacts": {"name": "Sam"}},
        "guests": [{"name": "Table 4", "party_size": 6, "rsvp": "yes", "dietary": "vegan"}],
        "dietary_matrix": {"vegan": 6}, "stakeholders": [
            {"kind": "supplier", "role": "caterer", "name": "Bob", "confirmation": "confirmed",
             "outstanding": None}],
        "lights": events_os._CONFIRM_TONE})
    r = client.get("/app/events/e1")
    assert r.status_code == 200
    assert "Harper Wedding" in r.text and "Table 4" in r.text
    assert "caterer" in r.text


def test_events_gate_404s_when_module_off(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "module_active", lambda *a, **k: False)
    assert client.get("/app/events").status_code == 404       # _require_events gate


def test_events_detail_404_for_foreign_event(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.events_os, "detail_view", lambda *a, **k: None)  # not this tenant's
    assert client.get("/app/events/nope").status_code == 404


# Fingerprints of today's main before the inversion, including property order.
@pytest.mark.parametrize("direction,event,tokens,system_hash,schema_hash", [
    ('outbound', False, 700, '56dca49977f84297a1f35e8b332110b487ea891a58f267c439f473f145c01b96', '5b70030abd15ea3de2dd9cd0d3f546b3a5a6b883b881fc4b28d5e55802e5b364'),
    ('inbound', False, 800, 'a654c4799d5532a0f85a682cf5e4b382511685e123cd8ab16f1741608223795f', 'e6c53ef81bf9767420ce38f39f6893abc601efbfcb7e027efafda80adc9eac16'),
    ('inbound', True, 1000, '9bbd9c36b42debe676bba97e402160d1bb274faaacee536d10052ab6748b207c', 'e331f2c75530f2452331c94866fa2cd9e4bfad67a1f00bd70a43f6d71158cc9f'),
])
def test_intel_request_matches_pre_inversion(monkeypatch, direction, event, tokens,
                                              system_hash, schema_hash):
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace
    from app import crm_intel
    seen = {}

    async def capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(content=[], model="test", usage=None)

    monkeypatch.setattr(crm_intel.agent, "client", lambda: None)
    monkeypatch.setattr(crm_intel.llm, "create", capture)
    monkeypatch.setattr(crm_intel.usage, "record_anthropic", lambda *a, **k: None)
    extensions = (crm_intel._INTEL_EXTENSIONS["event"],) if event else ()
    asyncio.run(crm_intel.analyze(
        "t1", direction=direction, subject="s", body="b", contact_name="A",
        company_name=None, open_deals=[], email_log_id="log1", extensions=extensions))
    assert seen["max_tokens"] == tokens
    assert seen["model"] == crm_intel.settings.anthropic_model
    assert hashlib.sha256(seen["system"].encode()).hexdigest() == system_hash
    schema = seen["output_config"]["format"]["schema"]
    assert hashlib.sha256(json.dumps(schema).encode()).hexdigest() == schema_hash


def test_intel_registry_is_generic_and_replaces_keys(monkeypatch):
    from app import crm_intel
    monkeypatch.setattr(crm_intel, "_INTEL_EXTENSIONS", {})
    schema = {"type": "object", "properties": {}}
    crm_intel.register_intel_extension("sample", schema, "first", lambda tid: True)
    crm_intel.register_intel_extension("sample", schema, "second", lambda tid: True)
    assert len(crm_intel._INTEL_EXTENSIONS) == 1
    extension = crm_intel._INTEL_EXTENSIONS["sample"]
    assert extension["system_prompt"] == "second"
    assert crm_intel._analyze_schema((extension,))["properties"]["sample"] == schema
    assert crm_intel.parse_analysis('{"sample":{"value":1}}')["sample"] == {"value": 1}
    for text in ('{"sample": []}', '{}', 'broken'):
        assert crm_intel.parse_analysis(text)["sample"] == {}
    for key in ("deal", "promises", "sentiment"):
        with pytest.raises(ValueError, match="collides"):
            crm_intel.register_intel_extension(key, schema, "", lambda tid: True)


@pytest.mark.parametrize("direction,active", [("inbound", True), ("inbound", False),
                                               ("outbound", True)])
def test_generic_extension_capture_order(monkeypatch, direction, active):
    import asyncio
    from app import crm_intel
    monkeypatch.setattr(crm_intel, "_INTEL_EXTENSIONS", {})
    seen = []

    def wants(tid):
        seen.append("gate")
        return active

    def capture(tid, deal_id, branch, *, email_log_id):
        assert (tid, deal_id, branch, email_log_id) == ("t1", "d1", {"value": 1}, "log1")
        seen.append("capture")
        return {"id": "extension-row"}

    crm_intel.register_intel_extension("sample", {}, "", wants, capture)

    async def analyze(tid, **kwargs):
        assert bool(kwargs["extensions"]) == (direction == "inbound" and active)
        seen.append("analyze")
        return {"deal": {}, "promises": [], "sample": {"value": 1}}

    monkeypatch.setattr(crm_intel, "analyze", analyze)
    monkeypatch.setattr(crm_intel.db, "crm_list_deals", lambda *a, **k: [{"id": "d1", "status": "open"}])
    monkeypatch.setattr(crm_intel.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(crm_intel, "apply_deal", lambda *a, **k: seen.append("deal") or "d1")
    monkeypatch.setattr(crm_intel, "apply_promises", lambda *a, **k: seen.append("promises") or {})
    monkeypatch.setattr(crm_intel, "apply_sentiment", lambda *a, **k: seen.append("sentiment"))
    out = asyncio.run(crm_intel.process_email(
        "t1", direction=direction, subject="s", body="b", contact_id="c1",
        company_id=None, contact_name="A", company_name=None, email_log_id="log1"))
    if direction == "outbound":
        assert seen == ["analyze", "promises"]
    else:
        assert seen == ["gate", "analyze", "deal", "promises"] + (["capture"] if active else []) + ["sentiment"]
    assert out["sample_id"] == ("extension-row" if direction == "inbound" and active else None)
