"""The Concierge edits and deletes DRAFT invoices and quotes - always on a confirm card.

Phase 1 of "edit and delete with a card first" (Kris, 15 October 2026), from scenario
N20 on the rig: "I put the wrong amount on one of Priya's invoices, a draft one. Can
you just bin it, or change it to two hundred quid?" There was no edit or delete kind,
so the Concierge made one up. Four kinds now exist: invoice_edit_draft,
invoice_delete_draft, quote_edit_draft, quote_delete_draft. Nothing else is in scope:
a sent invoice or quote, a contact, a booking and a setting all stay refusals.

The store below is an in-memory stand-in for the database with HONEST filter
semantics (eq, neq, is_, in_), so a missing tenant or status filter in a db helper
really does fail here. Its delete mirrors the schema's ON DELETE CASCADE from an
invoice (or quote) to its lines.

Run: python -m pytest tests/test_concierge_draft_edit_delete.py -q
"""
import asyncio
import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from app import concierge, concierge_actions, concierge_data, db, people
from app.auth import require_onboarded, TenantCtx
from app.main import app
from app.security import require_csrf_header

ROOT = Path(__file__).resolve().parent.parent

T1, T2 = "tenant-1", "tenant-2"
INV_1002 = "00000000-0000-4000-8000-000000001002"
INV_1004 = "00000000-0000-4000-8000-000000001004"
INV_9001 = "00000000-0000-4000-8000-000000009001"      # tenant 2's
QUO_1003 = "00000000-0000-4000-8000-000000001003"
QUO_1005 = "00000000-0000-4000-8000-000000001005"
QUO_9001 = "00000000-0000-4000-8000-000000009101"      # tenant 2's
BOOKING = "00000000-0000-4000-8000-00000000b001"

INVOICE_KINDS = ("invoice_edit_draft", "invoice_delete_draft")
QUOTE_KINDS = ("quote_edit_draft", "quote_delete_draft")
FOUR_KINDS = INVOICE_KINDS + QUOTE_KINDS


# ── an in-memory database with honest filters ─────────────────────────────────
class _Query:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.filters, self.op, self.payload = [], "select", None

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def eq(self, col, value):
        self.filters.append(lambda r, c=col, v=value: r.get(c) == v)
        return self

    def neq(self, col, value):
        self.filters.append(lambda r, c=col, v=value: r.get(c) != v)
        return self

    def is_(self, col, value):
        assert value == "null", "only is_(col, 'null') is modelled"
        self.filters.append(lambda r, c=col: r.get(c) is None)
        return self

    def in_(self, col, values):
        self.filters.append(lambda r, c=col, v=tuple(values): r.get(c) in v)
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        rows = self.store.s[self.name]
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "insert":
            new = [dict(r) for r in (self.payload if isinstance(self.payload, list) else [self.payload])]
            for r in new:
                r.setdefault("id", f"{self.name}-{len(rows) + 1}")
                if self.name == "invoices":          # the column defaults
                    r.setdefault("status", "draft")
                    r.setdefault("amount_paid", 0)
                if self.name == "quotes":
                    r.setdefault("status", "draft")
                    r.setdefault("version", 1)
                    r.setdefault("view_count", 0)
                    r.setdefault("reminders_sent", 0)
                rows.append(r)
            self.store.writes.append((self.name, "insert", len(new)))
            return SimpleNamespace(data=[dict(r) for r in new])
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            self.store.writes.append((self.name, "update", len(hit)))
            return SimpleNamespace(data=[dict(r) for r in hit])
        if self.op == "delete":
            gone = [r["id"] for r in hit]
            self.store.s[self.name] = [r for r in rows if r not in hit]
            # the schema's ON DELETE CASCADE: an invoice or quote takes its lines with it
            cascade = {"invoices": ("invoice_lines", "invoice_id"), "quotes": ("quote_lines", "quote_id")}
            if self.name in cascade:
                child, col = cascade[self.name]
                self.store.s[child] = [r for r in self.store.s[child] if r.get(col) not in gone]
            self.store.writes.append((self.name, "delete", len(hit)))
            return SimpleNamespace(data=[dict(r) for r in hit])
        return SimpleNamespace(data=[dict(r) for r in hit])


class _Store:
    def __init__(self):
        self.s = {"invoices": [], "invoice_lines": [], "quotes": [], "quote_lines": []}
        self.writes = []
        self.counters = {}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, fn, params):
        # models next_invoice_ref / next_quote_ref: a per-tenant counter that only goes up
        key = (fn, params["p_tenant_id"])
        self.counters[key] = self.counters.get(key, 0) + 1
        prefix = "INV-" if fn == "next_invoice_ref" else "QUO-"
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=prefix + str(1000 + self.counters[key])))


def _invoice(id_, ref, tenant=T1, status="draft", total=252.50, who="Priya Shah", **over):
    return {"id": id_, "tenant_id": tenant, "invoice_ref": ref, "status": status,
            "customer_email": "priya@example.test", "customer_name": who,
            "subtotal": total, "vat": 0.0, "total": total, "issue_date": "2026-10-05",
            "due_date": "2026-10-19", "sent_at": None, "paid_at": None, "payment_method": None,
            "stripe_payment_intent_id": None, "payment_link": None, "viewed_at": None,
            "amount_paid": 0, "prices_include_vat": None, **over}


def _line(table, parent, parent_col, desc, amount, tenant=T1, **over):
    return {"id": f"{table}-{parent}-{desc}", "tenant_id": tenant, parent_col: parent,
            "description": desc, "amount": amount, "quantity": 1, "booking_id": None, **over}


def _quote(id_, ref, tenant=T1, status="draft", total=252.50, who="Priya Shah", **over):
    return {"id": id_, "tenant_id": tenant, "quote_ref": ref, "status": status,
            "customer_email": "priya@example.test", "customer_name": who, "title": "Work",
            "subtotal": total, "vat": 0.0, "total": total, "version": 1, "sent_at": None,
            "viewed_at": None, "view_count": 0, "accepted_at": None, "declined_at": None,
            "invoice_id": None, "deposit_invoice_id": None, "reminders_sent": 0,
            "deposit_percent": None, "deposit_base": None, "prices_include_vat": None, **over}


@pytest.fixture
def rig(monkeypatch):
    store = _Store()
    store.s["invoices"] += [_invoice(INV_1002, "INV-1002"), _invoice(INV_1004, "INV-1004"),
                            _invoice(INV_9001, "INV-9001", tenant=T2, who="Someone Else")]
    for inv_id, tenant in ((INV_1002, T1), (INV_1004, T1), (INV_9001, T2)):
        store.s["invoice_lines"].append(_line("il", inv_id, "invoice_id", "Work as discussed", 252.50, tenant))
    settings = {"show_vat": False, "vat_rate": 20, "extras": {}}
    monkeypatch.setattr(db, "get_invoice_settings", lambda tenant_id, client=None: settings)
    monkeypatch.setattr(db, "db", lambda: store)
    events, proposals = [], []
    monkeypatch.setattr(db, "log_agent_event", lambda tenant_id, kind, summary, **kw: events.append(
        {"tenant_id": tenant_id, "kind": kind, "summary": summary, **kw}))

    def create(tenant_id, kind, summary, payload, **kw):
        proposals.append({"tenant_id": tenant_id, "kind": kind, "summary": summary, "payload": payload})
        return f"prop-{len(proposals)}"
    monkeypatch.setattr(db, "create_concierge_proposal", create)
    return SimpleNamespace(store=store, settings=settings, events=events, proposals=proposals)


@pytest.fixture
def client(monkeypatch):
    # The account's own owner (no People row), as the other route tests set it up.
    monkeypatch.setattr(db, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _stage(rig, action, member=None, tenant=T1):
    return concierge_actions.stage(tenant, rig.store, action, full_qa=False, scheduling_on=False,
                                   member=member)


def _run(rig, kind, payload, tenant=T1):
    return asyncio.run(concierge_actions.execute(tenant, rig.store, kind, payload))


def _invoice_row(rig, id_):
    return next((r for r in rig.store.s["invoices"] if r["id"] == id_), None)


def _lines_of(rig, id_, table="invoice_lines", col="invoice_id"):
    return sorted((l["description"], l["amount"]) for l in rig.store.s[table] if l[col] == id_)


# ═════════════════════════════════════════════════════════════════════════════
# the database helpers
# ═════════════════════════════════════════════════════════════════════════════
def test_update_draft_invoice_lines_replaces_the_lines_and_recomputes_the_totals(rig):
    rig.settings.update(show_vat=True, vat_rate=20)
    done = db.update_draft_invoice_lines(INV_1002, T1, [{"description": "Work as discussed", "amount": 200.0,
                                                         "booking_id": None}], client=rig.store)
    assert done is True
    row = _invoice_row(rig, INV_1002)
    assert (row["subtotal"], row["vat"], row["total"]) == (200.0, 40.0, 240.0)
    assert _lines_of(rig, INV_1002) == [("Work as discussed", 200.0)]
    # the other draft is untouched
    assert _lines_of(rig, INV_1004) == [("Work as discussed", 252.50)]


@pytest.mark.parametrize("status", ["sent", "overdue", "partially_paid", "paid", "cancelled", "void"])
def test_update_draft_invoice_lines_refuses_anything_but_a_draft_and_writes_nothing(rig, status):
    _invoice_row(rig, INV_1002)["status"] = status
    before_rows = copy.deepcopy(rig.store.s)
    done = db.update_draft_invoice_lines(INV_1002, T1, [{"description": "X", "amount": 1.0}], client=rig.store)
    assert done is False
    assert rig.store.s == before_rows          # not one line deleted, not one total moved


def test_update_draft_invoice_lines_is_tenant_scoped(rig):
    before_rows = copy.deepcopy(rig.store.s)
    # tenant 1 holds tenant 2's invoice id: nothing is found, nothing is written
    assert db.update_draft_invoice_lines(INV_9001, T1, [{"description": "X", "amount": 1.0}],
                                         client=rig.store) is False
    assert rig.store.s == before_rows


def test_the_card_preview_totals_are_what_the_save_stores(rig):
    for include in (None, True):
        rig.settings.update(show_vat=True, vat_rate=20, extras={"prices_include_vat": include})
        row = _invoice_row(rig, INV_1002)
        row["prices_include_vat"] = True if include else None
        lines = [{"description": "A", "amount": 100.01}, {"description": "B", "amount": 45.0}]
        preview = db.invoice_totals(T1, lines, include=row.get("prices_include_vat") is True, client=rig.store)
        db.update_invoice_lines(INV_1002, T1, lines, client=rig.store)
        row = _invoice_row(rig, INV_1002)
        assert preview == (row["subtotal"], row["vat"], row["total"])


def test_delete_draft_invoice_takes_the_row_and_its_lines_together(rig):
    assert db.delete_draft_invoice(INV_1004, T1, client=rig.store) is True
    assert _invoice_row(rig, INV_1004) is None
    assert _lines_of(rig, INV_1004) == []
    # the neighbour and the other tenant are untouched
    assert _invoice_row(rig, INV_1002) is not None and _lines_of(rig, INV_1002) == [("Work as discussed", 252.50)]
    assert _invoice_row(rig, INV_9001) is not None
    # ONE delete statement against the invoices table: the lines go by the schema's cascade
    assert [w for w in rig.store.writes if w[1] == "delete"] == [("invoices", "delete", 1)]


@pytest.mark.parametrize("over", [
    {"status": "sent"}, {"status": "overdue"}, {"status": "paid"}, {"status": "cancelled"},
    {"sent_at": "2026-10-06T09:00:00+00:00"},        # a sent invoice put back to draft
    {"paid_at": "2026-10-07T09:00:00+00:00"},
    {"payment_method": "bank_transfer"},
    {"stripe_payment_intent_id": "pi_1"},
    {"payment_link": "https://example.test/pay/1"},
    {"viewed_at": "2026-10-06T10:00:00+00:00"},
    {"amount_paid": 50},
], ids=lambda o: next(iter(o)) + "=" + str(next(iter(o.values())))[:12])
def test_delete_draft_invoice_refuses_anything_that_ever_left_the_building(rig, over):
    _invoice_row(rig, INV_1004).update(over)
    assert db.delete_draft_invoice(INV_1004, T1, client=rig.store) is False
    assert _invoice_row(rig, INV_1004) is not None and _lines_of(rig, INV_1004)


def test_delete_draft_invoice_is_tenant_scoped(rig):
    assert db.delete_draft_invoice(INV_9001, T1, client=rig.store) is False
    assert _invoice_row(rig, INV_9001) is not None and _lines_of(rig, INV_9001)


def test_a_deleted_invoice_number_is_never_issued_again(rig):
    """Refs come from a counter that only goes up (db/schema.sql next_invoice_ref:
    tenant_counters.invoice_seq + 1), never from max(ref)+1, so a hard delete of a
    draft cannot hand its number to a different document."""
    first = db.create_invoice(T1, "k@example.test", "K", None, None, [{"description": "A", "amount": 10.0}],
                              client=rig.store)
    second = db.create_invoice(T1, "k@example.test", "K", None, None, [{"description": "B", "amount": 20.0}],
                               client=rig.store)
    assert (first["invoice_ref"], second["invoice_ref"]) == ("INV-1001", "INV-1002")
    assert db.delete_draft_invoice(second["id"], T1, client=rig.store) is True
    third = db.create_invoice(T1, "k@example.test", "K", None, None, [{"description": "C", "amount": 30.0}],
                              client=rig.store)
    assert third["invoice_ref"] == "INV-1003"            # not INV-1002 again
    # the shipped functions are counters: this is the pin that keeps the model above true
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    for fn, col in (("next_invoice_ref", "invoice_seq"), ("next_quote_ref", "quote_seq")):
        body = schema[schema.index(f"function {fn}"):]
        body = body[:body.index("$$;") if "$$;" in body else body.index("end $$")]
        assert f"{col} = tenant_counters.{col} + 1" in body, fn
        assert "max(" not in body.lower(), f"{fn} must stay a counter, never max()+1"


def test_an_invoice_made_from_a_quote_says_which(rig):
    rig.store.s["quotes"] += [_quote(QUO_1003, "QUO-1003", status="invoiced", invoice_id=INV_1002),
                              _quote(QUO_1005, "QUO-1005", status="accepted", deposit_invoice_id=INV_1004),
                              _quote(QUO_9001, "QUO-9001", tenant=T2, invoice_id=INV_9001)]
    assert db.invoice_linked_quote_ref(INV_1002, T1, client=rig.store) == "QUO-1003"
    assert db.invoice_linked_quote_ref(INV_1004, T1, client=rig.store) == "QUO-1005"
    assert db.invoice_linked_quote_ref(INV_9001, T1, client=rig.store) is None      # tenant 2's quote
    rig.store.s["quotes"].clear()
    assert db.invoice_linked_quote_ref(INV_1002, T1, client=rig.store) is None


# ═════════════════════════════════════════════════════════════════════════════
# staging an edit
# ═════════════════════════════════════════════════════════════════════════════
def test_edit_stages_a_confirm_card_that_shows_before_and_after(rig):
    card = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": "INV-1002", "amount": "200"})
    assert card["kind"] == "invoice_edit_draft" and card["approval"] == "confirm"
    assert "INV-1002" in card["summary"] and "Priya Shah" in card["summary"]
    assert "£252.50 → £200.00" in card["summary"]
    shown = " | ".join(card["details"])
    assert "Work as discussed: £252.50 → £200.00" in shown
    assert "Total: £252.50 → £200.00" in shown
    assert "draft" in shown.lower() and "nothing has been sent" in shown.lower()
    saved = rig.proposals[0]
    assert saved["kind"] == "invoice_edit_draft" and saved["tenant_id"] == T1
    assert saved["payload"]["invoice_id"] == INV_1002
    assert saved["payload"]["lines"] == [{"description": "Work as discussed", "amount": 200.0, "booking_id": None}]
    # staging writes nothing to the invoice
    assert _lines_of(rig, INV_1002) == [("Work as discussed", 252.50)]
    assert _invoice_row(rig, INV_1002)["total"] == 252.50
    assert not [w for w in rig.store.writes if w[0].startswith("invoice")]


def test_edit_accepts_the_new_lines_as_text_json_or_a_list(rig):
    as_text = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                           "lines": "Parts | 120.50\nLabour | £79.50"})
    as_json = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                           "lines": '[{"description": "Parts", "amount": "120.50"}, '
                                    '{"description": "Labour", "amount": 79.5}]'})
    as_list = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                           "lines": [{"description": "Parts", "amount": "120.50"},
                                     {"description": "Labour", "amount": "79.50"}]})
    want = [("Parts", 120.5), ("Labour", 79.5)]
    for card, saved in zip((as_text, as_json, as_list), rig.proposals):
        assert [(l["description"], l["amount"]) for l in saved["payload"]["lines"]] == want
        assert "Total: £252.50 → £200.00" in " | ".join(card["details"])
    shown = " | ".join(as_text["details"])
    assert "Add: Labour — £79.50" in shown and "Parts" in shown


def test_edit_totals_use_the_same_vat_rules_as_the_main_app(rig):
    rig.settings.update(show_vat=True, vat_rate=20)
    _invoice_row(rig, INV_1002).update(subtotal=252.50, vat=50.50, total=303.00)
    card = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"})
    assert "Total: £303.00 → £240.00" in " | ".join(card["details"])
    assert "including £40.00 VAT" in " | ".join(card["details"])
    # an invoice made on the 'prices include VAT' basis keeps its own basis
    _invoice_row(rig, INV_1004).update(prices_include_vat=True, subtotal=210.42, vat=42.08, total=252.50)
    card = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1004, "amount": "240"})
    assert "Total: £252.50 → £240.00" in " | ".join(card["details"])


def test_edit_keeps_a_lines_link_to_its_booking_so_billed_work_is_not_freed(rig):
    rig.store.s["invoice_lines"] = [l for l in rig.store.s["invoice_lines"] if l["invoice_id"] != INV_1002]
    rig.store.s["invoice_lines"] += [
        _line("il", INV_1002, "invoice_id", "Parts", 100.0, booking_id=BOOKING),
        _line("il", INV_1002, "invoice_id", "Labour", 152.5)]
    _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                 "lines": "Parts | 100.00\nLabour | 100.00"})
    carried = {l["description"]: l["booking_id"] for l in rig.proposals[0]["payload"]["lines"]}
    assert carried == {"Parts": BOOKING, "Labour": None}
    # a reworded line in the same place still carries its link
    _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                 "lines": "Replacement parts | 100.00\nLabour | 152.50"})
    carried = {l["description"]: l["booking_id"] for l in rig.proposals[1]["payload"]["lines"]}
    assert carried == {"Replacement parts": BOOKING, "Labour": None}


def test_edit_removing_a_line_is_shown_as_removed(rig):
    rig.store.s["invoice_lines"] += [_line("il", INV_1002, "invoice_id", "Call out", 0.0)]
    rig.store.s["invoice_lines"][0]["amount"] = 252.50
    card = _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                        "lines": "Work as discussed | 252.50"})
    assert "Remove: Call out — £0.00" in " | ".join(card["details"])


@pytest.mark.parametrize("action, why", [
    ({"invoice_id": INV_1002}, "lines"),                                           # nothing to change to
    ({"invoice_id": INV_1002, "lines": "no amount on this line"}, "amount"),
    ({"invoice_id": INV_1002, "lines": "Work | lots"}, "amount"),
    ({"invoice_id": INV_1002, "lines": "Work | nan"}, "billable"),
    ({"invoice_id": INV_1002, "lines": "Work | 5000000"}, "billable"),
    ({"invoice_id": INV_1002, "lines": "| 20"}, "description"),
    ({"invoice_id": INV_1002, "lines": "[not json"}, "lines"),
    ({"invoice_id": INV_1002, "lines": "\n".join(f"Line {i} | 1" for i in range(25))}, "20 lines"),
    ({"invoice_id": INV_1002, "lines": "Work | 0"}, "£0.00"),
    ({"invoice_id": INV_1002, "lines": "Work | -5"}, "£0.00"),
    ({"invoice_id": INV_1002, "amount": "252.50"}, "nothing to change"),
    ({"invoice_id": INV_1002, "amount": "-3"}, "billable"),
], ids=lambda v: str(v)[:40])
def test_edit_refuses_what_it_cannot_prepare_in_plain_words(rig, action, why):
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_edit_draft", **action})
    assert why in str(e.value)
    assert not rig.proposals


def test_edit_with_only_an_amount_needs_the_full_lines_when_there_are_several(rig):
    rig.store.s["invoice_lines"] += [_line("il", INV_1002, "invoice_id", "Parts", 10.0)]
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"})
    text = str(e.value)
    assert "2 lines" in text and "Work as discussed" in text and "Parts" in text   # it says what is there


@pytest.mark.parametrize("status, expect", [
    ("sent", "already sent"), ("overdue", "already sent"), ("partially_paid", "already sent"),
    ("paid", "already been paid"), ("cancelled", "cancelled"), ("void", "void")])
def test_edit_refuses_a_sent_invoice_honestly(rig, status, expect):
    _invoice_row(rig, INV_1002)["status"] = status
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": "INV-1002", "amount": "200"})
    text = str(e.value)
    assert "INV-1002" in text and expect in text and "can't be edited from here" in text
    if expect == "already sent":
        assert "cancelling or voiding" in text.lower() and "isn't available from here yet" in text
    assert not rig.proposals


def test_edit_finds_the_invoice_by_ref_or_id_and_only_in_this_business(rig):
    assert _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": "inv-1002", "amount": "200"})
    assert rig.proposals[-1]["payload"]["invoice_id"] == INV_1002
    for ref in ("INV-9001", INV_9001, "INV-4040", ""):
        with pytest.raises(concierge_actions.ActionError) as e:
            _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": ref, "amount": "200"})
        assert "couldn't" in str(e.value)
    assert _lines_of(rig, INV_9001) == [("Work as discussed", 252.50)]


# ═════════════════════════════════════════════════════════════════════════════
# staging a delete
# ═════════════════════════════════════════════════════════════════════════════
def test_delete_card_says_exactly_what_goes(rig):
    card = _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": "INV-1004"})
    assert card["kind"] == "invoice_delete_draft" and card["approval"] == "confirm"
    said = card["summary"] + ". " + " ".join(card["details"])
    assert said == ("Delete draft INV-1004 (£252.50 for Priya Shah). "
                    "Nothing has been sent to the customer. It can't be brought back.")
    saved = rig.proposals[0]["payload"]
    assert saved["invoice_id"] == INV_1004 and saved["ref"] == "INV-1004" and saved["total"] == 252.5
    assert saved["lines"] == [{"description": "Work as discussed", "amount": 252.5}]   # the audit copy
    assert _invoice_row(rig, INV_1004) is not None                                        # nothing deleted yet


@pytest.mark.parametrize("status, expect", [("sent", "already sent"), ("overdue", "already sent"),
                                            ("paid", "already been paid"), ("cancelled", "cancelled")])
def test_delete_refuses_a_sent_invoice_honestly(rig, status, expect):
    _invoice_row(rig, INV_1004)["status"] = status
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": INV_1004})
    text = str(e.value)
    assert expect in text and "can't be deleted from here" in text
    if expect == "already sent":
        assert "cancelling or voiding" in text.lower() and "isn't available from here yet" in text
    assert not rig.proposals


@pytest.mark.parametrize("over, why", [
    ({"sent_at": "2026-10-06T09:00:00+00:00"}, "sent"),
    ({"payment_link": "https://example.test/pay/1"}, "payment link"),
    ({"paid_at": "2026-10-07T09:00:00+00:00"}, "payment"),
    ({"amount_paid": 20}, "payment"),
    ({"viewed_at": "2026-10-06T10:00:00+00:00"}, "opened by the customer"),
])
def test_delete_refuses_a_draft_that_ever_left_the_building(rig, over, why):
    _invoice_row(rig, INV_1004).update(over)
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": INV_1004})
    assert why in str(e.value) and "can't be deleted from here" in str(e.value)
    assert not rig.proposals


def test_the_words_and_the_delete_guard_read_the_same_facts(rig):
    """db.delete_draft_invoice guards on db.UNTOUCHED_INVOICE_NULLS; the sentence the owner
    reads names the same facts. Every guarded column has its clause, and the Python
    predicate, the sentence and the database guard agree on what counts as touched."""
    assert set(concierge_actions._INVOICE_TOUCHED) == set(db.UNTOUCHED_INVOICE_NULLS)
    fresh = _invoice("x", "INV-1")
    assert db.invoice_is_untouched_draft(fresh) and concierge_actions._invoice_touched_clause(fresh) is None
    for col in db.UNTOUCHED_INVOICE_NULLS:
        touched = {**fresh, col: "something"}
        assert not db.invoice_is_untouched_draft(touched), col
        assert concierge_actions._invoice_touched_clause(touched) == concierge_actions._INVOICE_TOUCHED[col]
    assert not db.invoice_is_untouched_draft({**fresh, "amount_paid": 1})
    assert concierge_actions._invoice_touched_clause({**fresh, "amount_paid": 1})
    assert not db.invoice_is_untouched_draft({**fresh, "status": "sent"})


def test_edit_matches_lines_by_description_first_so_a_reorder_keeps_each_link(rig):
    rig.store.s["invoice_lines"] = [l for l in rig.store.s["invoice_lines"] if l["invoice_id"] != INV_1002]
    rig.store.s["invoice_lines"] += [_line("il", INV_1002, "invoice_id", "Parts", 100.0, booking_id=BOOKING),
                                     _line("il", INV_1002, "invoice_id", "Labour", 152.5)]
    _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002,
                 "lines": "Labour | 150.00\nParts | 100.00"})        # swapped, and Labour changed
    carried = {l["description"]: (l["booking_id"], l["amount"]) for l in rig.proposals[0]["payload"]["lines"]}
    assert carried == {"Parts": (BOOKING, 100.0), "Labour": (None, 150.0)}


def test_delete_refuses_an_invoice_a_quote_points_at(rig):
    rig.store.s["quotes"].append(_quote(QUO_1003, "QUO-1003", status="invoiced", invoice_id=INV_1004))
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": INV_1004})
    assert "QUO-1003" in str(e.value) and "can't be deleted from here" in str(e.value)


def test_delete_cannot_reach_another_business(rig):
    for ref in (INV_9001, "INV-9001"):
        with pytest.raises(concierge_actions.ActionError, match="couldn't find that invoice"):
            _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": ref})
    assert _invoice_row(rig, INV_9001) is not None


# ═════════════════════════════════════════════════════════════════════════════
# always a card, and only for someone with the money door
# ═════════════════════════════════════════════════════════════════════════════
def _person(role="staff", pages=("today", "money"), can=()):
    return people.member({"user_id": "u1", "role": role, "active": True, "display_name": "Sam",
                          "scopes": {"pages": list(pages), "can": list(can)}})


def test_the_kinds_are_unlocked_on_every_tier_like_the_other_money_kinds():
    starter = concierge_actions.allowed_kinds(full_qa=False, scheduling_on=False)
    assert set(FOUR_KINDS) <= starter
    assert set(FOUR_KINDS) <= concierge_actions.allowed_kinds(full_qa=True, scheduling_on=True)


def test_the_money_door_is_the_one_invoice_create_uses():
    for kind in FOUR_KINDS:
        assert concierge_actions.ability_for(kind) == concierge_actions.ability_for("invoice_create") \
            == ("money", "send")
    from app import people as p
    assert "send" in [a for a, _l, _h in p.abilities_for("money")]


@pytest.mark.parametrize("kind", ["invoice_edit_draft", "invoice_delete_draft"])
def test_staff_without_the_money_tick_cannot_stage_it(rig, kind):
    action = {"kind": kind, "invoice_id": INV_1002, "amount": "200"}
    for who in (_person(pages=("today", "diary")),                 # money door shut
                _person(can=()),                                   # door open, no tick
                _person(role="accountant", pages=("money",))):     # read only
        with pytest.raises(concierge_actions.ActionError) as e:
            _stage(rig, action, member=who)
        assert "permission" in str(e.value)
    assert not rig.proposals
    # the tick opens it; so do an owner, a manager and an account from before People
    for who in (_person(can=("money:send",)), _person(role="owner", pages=people.PAGES),
                _person(role="manager", pages=people.PAGES), None):
        assert _stage(rig, action, member=who)["kind"] == kind


def test_a_delete_or_edit_can_never_be_automatic():
    """approval_mode is the seam for per-feature auto-triggers (Kris, 14 Oct). A change
    to or removal of a customer document is never one of them: whatever the future
    setting says, these stay on a card the owner taps."""
    assert set(FOUR_KINDS) <= concierge_actions.ALWAYS_CONFIRM_KINDS
    for kind in FOUR_KINDS:
        assert concierge_actions.approval_mode(kind) == "confirm"
    # and every kind this feature adds is a stager AND an executor AND timed
    from app import time_saved
    for kind in FOUR_KINDS:
        assert kind in concierge_actions._STAGERS and kind in concierge_actions._EXECUTORS
        assert time_saved.ACTION_MINUTES[kind] > 0
    src = (ROOT / "src/app/concierge_actions.py").read_text(encoding="utf-8")
    body = src[src.index("def approval_mode"):src.index("def allowed_kinds")]
    code = body.split('"""')[2]                       # what follows the docstring
    assert code.strip().startswith("if kind in ALWAYS_CONFIRM_KINDS:"), \
        "the always-confirm test must come before anything that could ever say 'auto'"


def test_a_staged_card_for_each_kind_names_the_confirm_mode(rig):
    for kind, extra in (("invoice_edit_draft", {"amount": "200"}), ("invoice_delete_draft", {})):
        card = _stage(rig, {"kind": kind, "invoice_id": INV_1002, **extra})
        assert card["approval"] == "confirm"


def test_the_kinds_ride_a_bulk_card_but_each_is_still_confirm_and_checked_on_its_own(rig):
    card = _stage(rig, {"kind": "bulk", "items": [
        {"kind": "invoice_delete_draft", "invoice_id": INV_1004},
        {"kind": "invoice_delete_draft", "invoice_id": INV_9001}]})        # not this business's
    assert card["kind"] == "bulk" and card["approval"] == "confirm"
    assert [i["ok"] for i in card["items"]] == [True, False]            # the other business's: refused
    assert "couldn't find" in card["items"][1]["error"]
    saved = rig.proposals[0]["payload"]["items"]
    assert saved[0]["payload"]["invoice_id"] == INV_1004 and saved[1]["ok"] is False


# ═════════════════════════════════════════════════════════════════════════════
# running an edit (only after the owner's Confirm)
# ═════════════════════════════════════════════════════════════════════════════
def _edit_payload(rig, **over):
    _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"})
    return {**rig.proposals[-1]["payload"], **over}


def test_confirming_an_edit_changes_exactly_that_draft(rig):
    payload = _edit_payload(rig)
    out = _run(rig, "invoice_edit_draft", payload)
    assert out["summary"] == "Done — invoice INV-1002 now totals £200.00 (it was £252.50). It is still a draft."
    assert out["link"] == {"label": "Open the invoice", "href": f"/app/invoices/{INV_1002}"}
    assert out.get("state") is None                                   # a plain success
    assert _lines_of(rig, INV_1002) == [("Work as discussed", 200.0)]
    assert _invoice_row(rig, INV_1002)["total"] == 200.0
    assert _invoice_row(rig, INV_1002)["status"] == "draft"
    # INV-1004 is the neighbour with the same amount: not touched
    assert _lines_of(rig, INV_1004) == [("Work as discussed", 252.50)]
    assert _invoice_row(rig, INV_1004)["total"] == 252.50
    assert [e["kind"] for e in rig.events] == ["invoice_edited"]
    assert "INV-1002" in rig.events[0]["summary"] and "confirmed by you" in rig.events[0]["summary"]


@pytest.mark.parametrize("status", ["sent", "paid", "cancelled", "void"])
def test_confirming_after_it_was_sent_in_the_meantime_refuses_and_changes_nothing(rig, status):
    payload = _edit_payload(rig)
    _invoice_row(rig, INV_1002)["status"] = status          # sent between the card and the tap
    before = copy.deepcopy(rig.store.s)
    out = _run(rig, "invoice_edit_draft", payload)
    assert out["state"] == "failed"                          # the route reports this honestly, not as 'unknown'
    assert "INV-1002" in out["summary"] and "nothing was changed" in out["summary"].lower()
    assert "no longer a draft" in out["summary"]
    assert rig.store.s == before and not rig.events


def test_confirming_after_the_lines_were_changed_elsewhere_refuses(rig):
    payload = _edit_payload(rig)
    rig.store.s["invoice_lines"][0]["amount"] = 300.0       # edited in the main app since the card
    out = _run(rig, "invoice_edit_draft", payload)
    assert out["state"] == "failed" and "changed since" in out["summary"]
    assert rig.store.s["invoice_lines"][0]["amount"] == 300.0 and not rig.events


def test_confirming_after_the_invoice_is_gone_refuses(rig):
    payload = _edit_payload(rig)
    db.delete_draft_invoice(INV_1002, T1, client=rig.store)
    out = _run(rig, "invoice_edit_draft", payload)
    assert out["state"] == "failed" and "isn't there any more" in out["summary"] and not rig.events


def test_the_same_edit_run_twice_applies_once(rig):
    payload = _edit_payload(rig)
    first = _run(rig, "invoice_edit_draft", payload)
    second = _run(rig, "invoice_edit_draft", payload)
    assert first.get("state") is None and second["state"] == "failed"
    assert _lines_of(rig, INV_1002) == [("Work as discussed", 200.0)]
    assert [e["kind"] for e in rig.events] == ["invoice_edited"]       # one event, not two
    assert len([w for w in rig.store.writes if w[:2] == ("invoice_lines", "insert")]) == 1


def test_a_send_landing_between_the_recheck_and_the_write_is_stopped_by_the_database(rig, monkeypatch):
    """The executor re-reads the invoice, then writes. There is no transaction, so the
    write itself is guarded on status too: a draft that was sent a moment after the
    re-read must not have its lines swapped."""
    payload = _edit_payload(rig)
    stale = dict(_invoice_row(rig, INV_1002))              # still a draft as the executor reads it
    _invoice_row(rig, INV_1002)["status"] = "sent"         # ...sent a moment later
    monkeypatch.setattr(concierge_actions.db, "get_invoice", lambda *a, **k: stale)
    before = copy.deepcopy(rig.store.s)
    out = _run(rig, "invoice_edit_draft", payload)
    assert out["state"] == "failed" and "no longer a draft" in out["summary"]
    assert rig.store.s == before and not rig.events


def test_an_edit_payload_aimed_at_another_business_cannot_write(rig):
    payload = {"invoice_id": INV_9001, "ref": "INV-9001", "who": "Someone Else", "total_before": 252.5,
               "total_after": 1.0, "before": [{"description": "Work as discussed", "amount": 252.5}],
               "lines": [{"description": "X", "amount": 1.0, "booking_id": None}]}
    out = _run(rig, "invoice_edit_draft", payload, tenant=T1)
    assert out["state"] == "failed"
    assert _lines_of(rig, INV_9001) == [("Work as discussed", 252.50)]
    assert _invoice_row(rig, INV_9001)["total"] == 252.5


# ═════════════════════════════════════════════════════════════════════════════
# running a delete
# ═════════════════════════════════════════════════════════════════════════════
def _delete_payload(rig, invoice_id=INV_1004, **over):
    _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": invoice_id})
    return {**rig.proposals[-1]["payload"], **over}


def test_confirming_a_delete_removes_exactly_that_draft_and_records_it(rig):
    payload = _delete_payload(rig)
    out = _run(rig, "invoice_delete_draft", payload)
    assert out["summary"] == ("Done — draft invoice INV-1004 (£252.50 for Priya Shah) is deleted. "
                              "It can't be brought back.")
    assert out["link"] == {"label": "Open your invoices", "href": "/app/invoices"}
    assert _invoice_row(rig, INV_1004) is None and _lines_of(rig, INV_1004) == []
    assert _invoice_row(rig, INV_1002) is not None and _lines_of(rig, INV_1002)       # INV-1002 stays
    assert _invoice_row(rig, INV_9001) is not None
    event = rig.events[0]
    assert event["kind"] == "invoice_deleted" and "INV-1004" in event["summary"]
    assert "£252.50" in event["summary"] and "confirmed by you" in event["summary"]
    assert event["contact"] == "priya@example.test"        # the log says whose draft went


def test_the_same_delete_run_twice_deletes_once(rig):
    payload = _delete_payload(rig)
    first = _run(rig, "invoice_delete_draft", payload)
    second = _run(rig, "invoice_delete_draft", payload)
    assert first.get("state") is None
    assert second["state"] == "failed" and "isn't there any more" in second["summary"]
    assert len(rig.events) == 1
    assert [w for w in rig.store.writes if w[1] == "delete"] == [("invoices", "delete", 1)]


@pytest.mark.parametrize("over, why", [
    ({"status": "sent"}, "no longer a draft"),
    ({"status": "paid"}, "no longer a draft"),
    ({"sent_at": "2026-10-06T09:00:00+00:00"}, "sent"),
    ({"payment_link": "https://example.test/pay/1"}, "payment link"),
    ({"amount_paid": 10}, "payment"),
    ({"total": 300.0}, "changed since"),
])
def test_confirming_a_delete_after_it_changed_refuses_and_keeps_the_draft(rig, over, why):
    payload = _delete_payload(rig)
    _invoice_row(rig, INV_1004).update(over)
    out = _run(rig, "invoice_delete_draft", payload)
    assert out["state"] == "failed" and why in out["summary"]
    assert "nothing was deleted" in out["summary"].lower()
    assert _invoice_row(rig, INV_1004) is not None and _lines_of(rig, INV_1004) and not rig.events


def test_confirming_a_delete_after_a_quote_took_it_over_refuses(rig):
    payload = _delete_payload(rig)
    rig.store.s["quotes"].append(_quote(QUO_1003, "QUO-1003", status="invoiced", invoice_id=INV_1004))
    out = _run(rig, "invoice_delete_draft", payload)
    assert out["state"] == "failed" and "QUO-1003" in out["summary"]
    assert _invoice_row(rig, INV_1004) is not None


def test_a_send_landing_between_the_recheck_and_the_delete_is_stopped_by_the_database(rig, monkeypatch):
    payload = _delete_payload(rig)
    stale = dict(_invoice_row(rig, INV_1004))
    _invoice_row(rig, INV_1004).update(status="sent", sent_at="2026-10-15T10:00:00+00:00")
    monkeypatch.setattr(concierge_actions.db, "get_invoice", lambda *a, **k: stale)
    out = _run(rig, "invoice_delete_draft", payload)
    assert out["state"] == "failed" and "nothing was deleted" in out["summary"]
    assert _invoice_row(rig, INV_1004) is not None and _lines_of(rig, INV_1004) and not rig.events


def test_a_delete_payload_aimed_at_another_business_cannot_delete(rig):
    payload = {"invoice_id": INV_9001, "ref": "INV-9001", "who": "Someone Else", "total": 252.5, "lines": []}
    out = _run(rig, "invoice_delete_draft", payload, tenant=T1)
    assert out["state"] == "failed"
    assert _invoice_row(rig, INV_9001) is not None and _lines_of(rig, INV_9001)


# ═════════════════════════════════════════════════════════════════════════════
# a refusal at Confirm reaches the owner in words (not as 'unconfirmed')
# ═════════════════════════════════════════════════════════════════════════════
def test_a_bulk_run_reports_a_refused_item_as_failed_not_done(rig):
    payload = {"items": [
        {"kind": "invoice_delete_draft", "ok": True, "payload": _delete_payload(rig, INV_1004)},
        {"kind": "invoice_delete_draft", "ok": True, "payload": _delete_payload(rig, INV_1002)}]}
    _invoice_row(rig, INV_1002)["status"] = "sent"               # sent between the card and the tap
    out = asyncio.run(concierge_actions.execute(T1, rig.store, "bulk", payload))
    assert [(r["index"], r["ok"], r["state"]) for r in out["results"]] == [(0, True, "completed"),
                                                                            (1, False, "failed")]
    assert "no longer a draft" in out["results"][1]["summary"]
    assert out["summary"].startswith("1 of 2 items completed.") and out["state"] == "completed"
    assert _invoice_row(rig, INV_1004) is None and _invoice_row(rig, INV_1002) is not None


def test_the_route_tells_the_owner_why_a_refused_confirm_changed_nothing(client, monkeypatch):
    from proposal_receipts import execution_result
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda pid, tid, max_age_s=300, client=None, **kw: {
                            "id": pid, "result": execution_result(pid, kw.get("execution_input")),
                            "kind": "invoice_delete_draft", "payload": {"invoice_id": INV_1004}})
    finished = {}
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal",
                        lambda pid, tid, status, result=None, client=None, **kw:
                        finished.update(status=status) or True)
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: None)
    sentence = "Invoice INV-1004 is no longer a draft, so nothing was deleted."

    async def refuse(*a, **k):
        return {"summary": sentence, "state": "failed"}
    monkeypatch.setattr(concierge_routes.concierge_actions, "execute", refuse)
    res = client.post("/concierge/business/execute",
                      json={"proposal_id": "0b6f8f9e-1111-2222-3333-444455556666", "confirm_via": "tap"})
    data = res.json()
    assert (data["ok"], data["state"], data["summary"]) == (False, "failed", sentence)
    assert finished["status"] == "failed"


# ═════════════════════════════════════════════════════════════════════════════
# the model can see what it is editing
# ═════════════════════════════════════════════════════════════════════════════
def test_list_invoices_hands_the_model_a_drafts_lines_and_nobody_elses(rig, monkeypatch):
    rig.store.s["invoices"] = [i for i in rig.store.s["invoices"] if i["tenant_id"] == T1]
    _invoice_row(rig, INV_1004)["status"] = "sent"
    rig.store.s["invoice_lines"] += [_line("il", INV_1002, "invoice_id", "Parts", 10.0)]
    monkeypatch.setattr(db, "list_invoices", lambda tenant_id, client=None: rig.store.s["invoices"])
    out = concierge_data._list_invoices(T1, rig.store, {})
    by_ref = {i["ref"]: i for i in out["invoices"]}
    assert sorted((l["description"], l["amount"]) for l in by_ref["INV-1002"]["lines"]) == \
        [("Parts", 10.0), ("Work as discussed", 252.5)]
    assert "lines" not in by_ref["INV-1004"]                  # a sent invoice's lines are not offered for edit


def test_the_time_saved_table_knows_the_new_kinds():
    from app import time_saved
    for kind in ("invoice_edit_draft", "invoice_delete_draft"):
        assert time_saved.ACTION_MINUTES[kind] > 0


# ═════════════════════════════════════════════════════════════════════════════
# the Confirm route, end to end with the real executors
# ═════════════════════════════════════════════════════════════════════════════
def _confirm_through_the_route(client, monkeypatch, rig, kind, payload):
    """POST /concierge/business/execute with a claimed proposal of this kind, the real
    executor running against the in-memory store. Returns (response json, receipt status)."""
    from proposal_receipts import execution_result
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda pid, tid, max_age_s=300, client=None, **kw: {
                            "id": pid, "result": execution_result(pid, kw.get("execution_input")),
                            "kind": kind, "payload": payload})
    finished = {}
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal",
                        lambda pid, tid, status, result=None, client=None, **kw:
                        finished.update(status=status, result=result) or True)
    saved = []
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tid, role, content, **kw: saved.append(content))

    real = concierge_routes.concierge_actions.execute

    async def run_with_store(tenant_id, _client, kind_, payload_, **kw):
        return await real(tenant_id, rig.store, kind_, payload_, **kw)    # the in-memory store stands in for the database
    monkeypatch.setattr(concierge_routes.concierge_actions, "execute", run_with_store)
    res = client.post("/concierge/business/execute",
                      json={"proposal_id": "0b6f8f9e-1111-2222-3333-444455556666", "confirm_via": "tap"})
    return res.json(), finished.get("status"), saved


@pytest.fixture
def owner_client(monkeypatch):
    """The route's caller is the owner of the rig's first business (tenant-1)."""
    monkeypatch.setattr(db, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=T1, client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_route_runs_a_confirmed_delete_and_reports_it(owner_client, monkeypatch, rig):
    _stage(rig, {"kind": "invoice_delete_draft", "invoice_id": INV_1004})
    payload = rig.proposals[-1]["payload"]
    data, status, saved = _confirm_through_the_route(owner_client, monkeypatch, rig, "invoice_delete_draft", payload)
    assert (data["ok"], data["state"]) == (True, "completed") and status == "executed"
    assert data["summary"].startswith("Done — draft invoice INV-1004") and "can't be brought back" in data["summary"]
    assert _invoice_row(rig, INV_1004) is None and _invoice_row(rig, INV_1002) is not None
    assert any("INV-1004" in s for s in saved)                 # the outcome joins the transcript


def test_the_route_shows_the_owner_a_refusal_from_the_real_executor(owner_client, monkeypatch, rig):
    _stage(rig, {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"})
    payload = rig.proposals[-1]["payload"]
    _invoice_row(rig, INV_1002)["status"] = "sent"             # sent between the card and the tap
    data, status, _saved = _confirm_through_the_route(owner_client, monkeypatch, rig, "invoice_edit_draft", payload)
    assert (data["ok"], data["state"]) == (False, "failed") and status == "failed"
    assert "INV-1002" in data["summary"] and "no longer a draft" in data["summary"]
    assert "nothing was changed" in data["summary"].lower()
    assert _lines_of(rig, INV_1002) == [("Work as discussed", 252.50)]
