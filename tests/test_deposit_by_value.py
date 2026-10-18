"""Deposit by value: a deposit rule on a document's amount ("over X, ask for N%").

Stored as extras.deposit_rules beside peak_deposits; ONE function decides the
percentage (invoice_config.deposit_percent_for) and every place that asks for a
deposit routes through it. With no rules every existing behaviour is unchanged.

Run: python -m pytest tests/test_deposit_by_value.py -q
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, deposits, invoice_config
from app.invoice_config import clean_deposit_rules, deposit_percent_for

PEAK = [{"from": "2026-12-18", "to": "2027-01-03", "percent": 40}]
XMAS = (date(2026, 12, 20), date(2026, 12, 22))


def _s(default=None, rules=None, peaks=None):
    return {"default_deposit_percent": default, "deposit_rules": rules or [],
            "peak_deposits": peaks or []}


# ── cleaning ──────────────────────────────────────────────────────────────────
def test_cleaning_drops_invalid_rows_sorts_and_caps():
    raw = [{"over": 500, "percent": 30}, {"over": "250", "percent": "50"},
           {"over": 0, "percent": 50}, {"over": -5, "percent": 50},
           {"over": 100, "percent": 0}, {"over": 100, "percent": 101},
           {"over": "abc", "percent": 10}, {"over": None, "percent": 10},
           {"over": float("nan"), "percent": 10}, {"over": float("inf"), "percent": 10},
           "junk", None, {"percent": 10}]
    assert clean_deposit_rules(raw) == [{"over": 250.0, "percent": 50.0},
                                        {"over": 500.0, "percent": 30.0}]
    assert clean_deposit_rules({"over": 1}) == []
    assert clean_deposit_rules(None) == []
    many = [{"over": i, "percent": 10} for i in range(30, 0, -1)]
    got = clean_deposit_rules(many)
    assert len(got) == 10 and [r["over"] for r in got] == [float(i) for i in range(1, 11)]
    assert clean_deposit_rules([{"over": 100, "percent": 100}]) == [{"over": 100.0, "percent": 100.0}]


def test_settings_for_carries_the_rules_and_defaults_to_none():
    assert invoice_config.settings_for({})["deposit_rules"] == []
    s = invoice_config.settings_for({"extras": {"deposit_rules": [{"over": 250, "percent": 50}, {"over": 0, "percent": 5}]}})
    assert s["deposit_rules"] == [{"over": 250.0, "percent": 50.0}]


# ── selection ─────────────────────────────────────────────────────────────────
def test_none():
    assert deposit_percent_for(_s(), 1000) == (None, None)
    assert deposit_percent_for(_s(), None) == (None, None)
    assert deposit_percent_for({}, 1000, *XMAS) == (None, None)


def test_default_only():
    assert deposit_percent_for(_s(default=25), 10) == (25.0, "default")
    assert deposit_percent_for(_s(default=25), None) == (25.0, "default")
    assert deposit_percent_for(_s(default=0), 10) == (None, None)
    assert deposit_percent_for(_s(default=150), 10) == (None, None)


def test_value_only_over_is_strictly_greater():
    s = _s(rules=[{"over": 250, "percent": 50}])
    assert deposit_percent_for(s, 249.99) == (None, None)
    assert deposit_percent_for(s, 250) == (None, None)            # at the threshold: no
    assert deposit_percent_for(s, 250.01) == (50.0, "value")
    assert deposit_percent_for(s, "300") == (50.0, "value")
    assert deposit_percent_for(s, None) == (None, None)
    assert deposit_percent_for(s, "nope") == (None, None)


def test_two_value_rules_the_highest_over_exceeded_wins():
    s = _s(rules=[{"over": 250, "percent": 50}, {"over": 1000, "percent": 30}])
    assert deposit_percent_for(s, 300) == (50.0, "value")
    assert deposit_percent_for(s, 1000) == (50.0, "value")         # not over 1000
    assert deposit_percent_for(s, 1000.5) == (30.0, "value")       # the larger threshold, even though lower %
    assert deposit_percent_for(s, 100) == (None, None)


def test_peak_only():
    s = _s(peaks=PEAK)
    assert deposit_percent_for(s, 100, *XMAS) == (40.0, "peak")
    assert deposit_percent_for(s, 100, date(2026, 11, 1), date(2026, 11, 3)) == (None, None)
    assert deposit_percent_for(s, 100) == (None, None)             # no dates, no peak (quotes)


def test_peak_and_value_the_higher_wins():
    hi_value = _s(rules=[{"over": 100, "percent": 60}], peaks=PEAK)
    assert deposit_percent_for(hi_value, 200, *XMAS) == (60.0, "value")
    lo_value = _s(rules=[{"over": 100, "percent": 20}], peaks=PEAK)
    assert deposit_percent_for(lo_value, 200, *XMAS) == (40.0, "peak")
    tie = _s(rules=[{"over": 100, "percent": 40}], peaks=PEAK)
    assert deposit_percent_for(tie, 200, *XMAS) == (40.0, "value")
    # the value rule doesn't match: the peak alone
    assert deposit_percent_for(lo_value, 50, *XMAS) == (40.0, "peak")


def test_default_and_value():
    s = _s(default=25, rules=[{"over": 250, "percent": 50}])
    assert deposit_percent_for(s, 100) == (25.0, "default")
    assert deposit_percent_for(s, 250) == (25.0, "default")
    assert deposit_percent_for(s, 251) == (50.0, "value")
    # a value rule is the more specific: it wins over the default even when lower
    low = _s(default=50, rules=[{"over": 250, "percent": 10}])
    assert deposit_percent_for(low, 300) == (10.0, "value")


def test_default_and_peak_the_peak_only_when_higher_as_before():
    assert deposit_percent_for(_s(default=25, peaks=PEAK), 100, *XMAS) == (40.0, "peak")
    assert deposit_percent_for(_s(default=50, peaks=PEAK), 100, *XMAS) == (50.0, "default")
    assert deposit_percent_for(_s(default=40, peaks=PEAK), 100, *XMAS) == (40.0, "default")


# ── the form round-trip ───────────────────────────────────────────────────────
def test_form_round_trip():
    from starlette.datastructures import FormData
    form = FormData([("payments_form", "1"),
                     ("deposit_rule_over", "1000"), ("deposit_rule_percent", "30"),
                     ("deposit_rule_over", "250"), ("deposit_rule_percent", "50"),
                     ("deposit_rule_over", ""), ("deposit_rule_percent", "20"),     # blank row dropped
                     ("deposit_rule_over", "99"), ("deposit_rule_percent", "0")])   # invalid dropped
    out = invoice_config.clean_form(form)
    assert out["extras"]["deposit_rules"] == [{"over": 250.0, "percent": 50.0}, {"over": 1000.0, "percent": 30.0}]
    # what was saved reads back through settings_for
    back = invoice_config.settings_for({"extras": out["extras"]})
    assert back["deposit_rules"] == out["extras"]["deposit_rules"]
    # a plain dict post (lists) works too
    d = invoice_config.clean_form({"payments_form": "1", "deposit_rule_over": ["300"], "deposit_rule_percent": ["25"]})
    assert d["extras"]["deposit_rules"] == [{"over": 300.0, "percent": 25.0}]


def test_form_removing_all_rows_clears_them_and_another_section_leaves_them_alone():
    assert invoice_config.clean_form({"payments_form": "1"})["extras"]["deposit_rules"] == []
    assert "deposit_rules" not in invoice_config.clean_form({}).get("extras", {})


def test_the_settings_page_has_the_block_and_shows_saved_rules():
    t = (Path(__file__).resolve().parent.parent / "src/app/templates/app/invoice_settings.html").read_text(encoding="utf-8")
    assert 'name="deposit_rule_over"' in t and 'name="deposit_rule_percent"' in t
    assert "Deposit by value" in t and "deposit-rule-add" in t and "deposit-rule-remove" in t


# ── a quote: empty rules == today ─────────────────────────────────────────────
def _make_quote(monkeypatch, raw, total, **kw):
    written = {}

    class _Ins:
        def insert(self, row):
            if isinstance(row, dict):
                written.update(row)
            return self

        def execute(self):
            return SimpleNamespace(data=[{**written, "id": "q1"}])

    class _Client:
        def table(self, name):
            return _Ins()

    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: raw)
    monkeypatch.setattr(db, "_quote_totals", lambda t, lines, client=None: (total / 1.2, total - total / 1.2, total))
    monkeypatch.setattr(db, "next_quote_ref", lambda t, client=None: "QUO-1001")
    monkeypatch.setattr(db, "deal_id_for_job", lambda *a, **k: None)
    monkeypatch.setattr(db, "open_deal_id_for_contact", lambda *a, **k: None)
    db.create_quote("t1", "k@x.co", "Kris", [], client=_Client(), **kw)
    return written["deposit_percent"]


@pytest.mark.parametrize("raw,expect", [
    ({}, None),
    ({"default_deposit_percent": 25}, 25.0),
    ({"default_deposit_percent": "25", "extras": {}}, 25.0),
    ({"default_deposit_percent": 0}, None),
    ({"default_deposit_percent": 150}, None),
    ({"default_deposit_percent": 25, "extras": {"deposit_rules": []}}, 25.0),
])
def test_a_quote_with_no_rules_is_exactly_as_before(monkeypatch, raw, expect):
    assert _make_quote(monkeypatch, raw, 500.0) == expect


def test_a_quote_asks_for_the_rule_on_its_total(monkeypatch):
    raw = {"default_deposit_percent": 10, "extras": {"deposit_rules": [{"over": 250, "percent": 50}]}}
    assert _make_quote(monkeypatch, raw, 250.0) == 10.0            # at the threshold: the default
    assert _make_quote(monkeypatch, raw, 250.01) == 50.0
    assert _make_quote(monkeypatch, {"extras": raw["extras"]}, 100.0) is None   # no default, rule not met
    # an explicit deposit on the quote still beats everything; 0 forces none
    assert _make_quote(monkeypatch, raw, 900.0, deposit_percent=20) == 20
    assert _make_quote(monkeypatch, raw, 900.0, deposit_percent=0) is None


# ── a confirmed booking: empty rules == today; a rule sizes the amount ────────
def _wire(monkeypatch, settings, bookings):
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: settings)
    monkeypatch.setattr(db, "bookings_for_log", lambda *a, **k: bookings)
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: set())


def _bk(i, amount, day="2026-10-07", **kw):
    return {"id": i, "booking_ref": f"BK-{i}", "status": "proposed", "quoted_amount": amount,
            "deposit_amount": None, "summary": "S", "booking_date": day, **kw}


def test_confirmed_bookings_with_no_rules_are_unchanged(monkeypatch):
    _wire(monkeypatch, {"default_deposit_percent": 25, "extras": {"peak_deposits": [
        {"from": "2026-12-18", "to": "2027-01-03", "percent": 100}]}},
        [_bk("b1", 30.5), _bk("b2", 61.0, day="2026-12-23"), _bk("b3", 40.0, deposit_amount=5)])
    _s2, _held, lines = deposits._deposit_lines("t1", "L", "c@x.co")
    by = {l["booking_id"]: l for l in lines}
    assert by["b1"]["amount"] == 7.63 and by["b1"]["description"].startswith("25% deposit")
    assert by["b2"]["amount"] == 61.0 and "100% deposit (peak dates)" in by["b2"]["description"]
    assert by["b3"]["amount"] == 5.0 and by["b3"]["description"].startswith("Deposit ")
    # no setting at all: nothing asked for
    _wire(monkeypatch, {"extras": {}}, [_bk("b1", 30.5)])
    assert deposits._deposit_lines("t1", "L", "c@x.co") is None


def test_a_value_rule_sizes_a_booking_deposit_and_the_preview_shows_it(monkeypatch):
    _wire(monkeypatch, {"default_deposit_percent": 10, "extras": {"deposit_rules": [{"over": 250, "percent": 50}]}},
          [_bk("b1", 200.0), _bk("b2", 300.0), _bk("b3", 250.0)])
    _s2, _held, lines = deposits._deposit_lines("t1", "L", "c@x.co")
    by = {l["booking_id"]: l["amount"] for l in lines}
    assert by == {"b1": 20.0, "b2": 150.0, "b3": 25.0}
    from app import payments
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: True)
    got = deposits.preview_for_log("t1", "L", "c@x.co")
    assert got["total"] == 195.0 and got["refs"] == "BK-b1, BK-b2, BK-b3"


def test_value_rule_alone_asks_nothing_below_it_and_peak_beats_a_lower_rule(monkeypatch):
    _wire(monkeypatch, {"extras": {"deposit_rules": [{"over": 250, "percent": 50}]}},
          [_bk("b1", 200.0), _bk("b2", 300.0)])
    _s2, _held, lines = deposits._deposit_lines("t1", "L", "c@x.co")
    assert [(l["booking_id"], l["amount"]) for l in lines] == [("b2", 150.0)]
    _wire(monkeypatch, {"extras": {"deposit_rules": [{"over": 250, "percent": 20}],
                                   "peak_deposits": [{"from": "2026-12-18", "to": "2027-01-03", "percent": 100}]}},
          [_bk("b1", 300.0, day="2026-12-23")])
    _s2, _held, lines = deposits._deposit_lines("t1", "L", "c@x.co")
    assert lines[0]["amount"] == 300.0 and "(peak dates)" in lines[0]["description"]


def test_a_rule_never_decides_whether_a_confirmation_is_asked(monkeypatch):
    _wire(monkeypatch, {"default_deposit_percent": 10, "extras": {"deposit_on_confirm": False,
                        "deposit_rules": [{"over": 250, "percent": 50}]}}, [_bk("b1", 900.0)])
    assert deposits._deposit_lines("t1", "L", "c@x.co") is None


# ═══ Step 2: a rule on SOME lines only ("ticked" basis) ═══════════════════════
from app import quoting  # noqa: E402


def _t(rules):
    return {"deposit_rules": rules, "peak_deposits": [], "default_deposit_percent": None}


def test_basis_is_cleaned_and_a_rule_without_one_is_total():
    got = clean_deposit_rules([{"over": 100, "percent": 10},
                               {"over": 200, "percent": 20, "basis": "ticked"},
                               {"over": 300, "percent": 30, "basis": "total"},
                               {"over": 400, "percent": 40, "basis": "junk"}])
    assert got == [{"over": 100.0, "percent": 10.0}, {"over": 200.0, "percent": 20.0, "basis": "ticked"},
                   {"over": 300.0, "percent": 30.0}, {"over": 400.0, "percent": 40.0}]
    # stored step-1 rules (no basis) test the whole amount and ignore the ticked sum
    s = _t([{"over": 250, "percent": 50}])
    assert deposit_percent_for(s, 300) == (50.0, "value")
    assert deposit_percent_for(s, 100, ticked_amount=900) == (None, None)
    assert invoice_config.deposit_rule_for(s, 300) == {"over": 250.0, "percent": 50.0}


def test_ticked_rule_is_tested_on_the_ticked_amount_only():
    s = _t([{"over": 250, "percent": 50, "basis": "ticked"}])
    assert deposit_percent_for(s, 5000) == (None, None)                    # no ticked lines: no match
    assert deposit_percent_for(s, 5000, ticked_amount=None) == (None, None)
    assert deposit_percent_for(s, 5000, ticked_amount=0) == (None, None)
    assert deposit_percent_for(s, 5000, ticked_amount=250) == (None, None)  # strictly over
    assert deposit_percent_for(s, 100, ticked_amount=250.01) == (50.0, "value")


def test_mixed_bases_the_highest_over_among_those_that_match():
    s = _t([{"over": 250, "percent": 50, "basis": "ticked"}, {"over": 1000, "percent": 20}])
    assert deposit_percent_for(s, 1200, ticked_amount=300) == (20.0, "value")
    assert invoice_config.deposit_rule_for(s, 1200, 300).get("basis") != "ticked"
    assert invoice_config.deposit_rule_for(s, 900, 300)["basis"] == "ticked"
    assert deposit_percent_for(s, 900, ticked_amount=100) == (None, None)
    assert deposit_percent_for(s, 1200, ticked_amount=100) == (20.0, "value")


def test_form_round_trip_with_basis():
    from starlette.datastructures import FormData
    form = FormData([("payments_form", "1"),
                     ("deposit_rule_over", "250"), ("deposit_rule_percent", "50"), ("deposit_rule_basis", "ticked"),
                     ("deposit_rule_over", "1000"), ("deposit_rule_percent", "30"), ("deposit_rule_basis", "total")])
    out = invoice_config.clean_form(form)["extras"]["deposit_rules"]
    assert out == [{"over": 250.0, "percent": 50.0, "basis": "ticked"}, {"over": 1000.0, "percent": 30.0}]
    assert invoice_config.settings_for({"extras": {"deposit_rules": out}})["deposit_rules"] == out
    t = (Path(__file__).resolve().parent.parent / "src/app/templates/app/invoice_settings.html").read_text(encoding="utf-8")
    assert "of the whole amount" in t and "of the ticked lines only" in t and 'name="deposit_rule_basis"' in t


def test_the_editor_row_has_the_tick():
    root = Path(__file__).resolve().parent.parent / "src/app/templates/app"
    for name in ("quote.html", "quote_new.html"):
        t = (root / name).read_text(encoding="utf-8")
        assert "Counts towards the deposit rule" in t and 'name="counts_deposit"' in t
    from starlette.datastructures import FormData

    from app import dashboard
    f = FormData([("description", "A"), ("amount", "10"), ("counts_deposit", "1"),
                  ("description", "B"), ("amount", "5"), ("counts_deposit", "0"),
                  ("description", "C"), ("amount", "1")])
    assert [l.get("counts_to_deposit_rule", False) for l in dashboard._quote_lines_from_form(f)] == [True, False, False]


# ── an in-memory quotes / quote_lines store, to run create -> edit -> accept ──
class _Q:
    def __init__(self, store, name):
        self.s, self.name, self.f, self.op, self.payload = store, name, [], "select", None

    def select(self, *_a):
        return self

    def eq(self, k, v):
        self.f.append((k, v))
        return self

    def limit(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def is_(self, *_a):
        return self

    def insert(self, p):
        self.op, self.payload = "insert", p
        return self

    def update(self, p):
        self.op, self.payload = "update", p
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        rows = self.s[self.name]
        hit = [r for r in rows if all(r.get(k) == v for k, v in self.f)]
        if self.op == "insert":
            new = [dict(r) for r in (self.payload if isinstance(self.payload, list) else [self.payload])]
            for r in new:
                r.setdefault("id", f"{self.name}-{len(rows) + 1}")
                if self.name == "quotes":
                    r.setdefault("status", "sent")
                    r.setdefault("version", 1)
                rows.append(r)
            return SimpleNamespace(data=[dict(r) for r in new])
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hit])
        if self.op == "delete":
            self.s[self.name] = [r for r in rows if r not in hit]
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[dict(r) for r in hit])


class _Store:
    def __init__(self):
        self.s = {"quotes": [], "quote_lines": []}

    def table(self, name):
        return _Q(self.s, name)


def _rig(monkeypatch, settings, vat=False):
    store = _Store()
    raw = {"extras": settings.get("extras", {}), "default_deposit_percent": settings.get("default"),
           "show_vat": vat, "vat_rate": 20}
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: raw)
    monkeypatch.setattr(db, "next_quote_ref", lambda t, client=None: "QUO-1001")
    monkeypatch.setattr(db, "deal_id_for_job", lambda *a, **k: None)
    monkeypatch.setattr(db, "open_deal_id_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "db", lambda: store)
    return store


RULE_T = {"extras": {"deposit_rules": [{"over": 250, "percent": 50, "basis": "ticked"}]}}


def _ln(desc, amount, ticked=False, **kw):
    return {"description": desc, "amount": amount, "counts_to_deposit_rule": ticked, **kw}


def test_ticked_lines_over_the_threshold_take_the_percent_of_the_ticked_net(monkeypatch):
    store = _rig(monkeypatch, RULE_T)
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 300, True), _ln("Labour", 700)], client=store)
    assert q["deposit_percent"] == 50.0 and q["deposit_base"] == 300.0
    assert quoting.deposit_net_amount({**q, "subtotal": 1000.0}) == 150.0     # not 500
    assert [l.get("counts_to_deposit_rule", False) for l in store.s["quote_lines"]] == [True, False]


def test_ticked_lines_under_the_threshold_or_none_ticked_ask_nothing(monkeypatch):
    store = _rig(monkeypatch, RULE_T)
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 200, True), _ln("Labour", 5000)], client=store)
    assert q["deposit_percent"] is None and "deposit_base" not in q
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 900), _ln("Labour", 5000)], client=store)
    assert q["deposit_percent"] is None and "deposit_base" not in q
    # a deselected optional ticked line does not count
    q = db.create_quote("t1", "k@x.co", "K",
                        [_ln("Parts", 900, True, is_optional=True, is_selected=False)], client=store)
    assert q["deposit_percent"] is None


def test_the_threshold_is_tested_gross_and_the_base_is_net(monkeypatch):
    store = _rig(monkeypatch, RULE_T, vat=True)
    # net 220 -> gross 264 (> 250): the rule matches; the base stays the NET 220
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 220, True)], client=store)
    assert q["deposit_percent"] == 50.0 and q["deposit_base"] == 220.0
    # net 200 -> gross 240: no match
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 200, True)], client=store)
    assert q["deposit_percent"] is None


def test_total_rules_and_the_default_store_no_base_and_write_no_new_keys(monkeypatch):
    store = _rig(monkeypatch, {"extras": {"deposit_rules": [{"over": 250, "percent": 50}]}, "default": 10})
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 300, True), _ln("Labour", 700)], client=store)
    assert q["deposit_percent"] == 50.0 and "deposit_base" not in q
    q = db.create_quote("t1", "k@x.co", "K", [{"description": "X", "amount": 10}], client=store)
    assert q["deposit_percent"] == 10.0 and "deposit_base" not in q
    assert "counts_to_deposit_rule" not in store.s["quote_lines"][-1]   # an old-style line writes what it always did
    assert "deposit_base" not in {k for r in store.s["quotes"] for k in r}


def test_mixed_rules_of_different_bases_on_a_quote(monkeypatch):
    store = _rig(monkeypatch, {"extras": {"deposit_rules": [
        {"over": 250, "percent": 50, "basis": "ticked"}, {"over": 2000, "percent": 20}]}})
    q = db.create_quote("t1", "k@x.co", "K", [_ln("P", 300, True), _ln("L", 2000)], client=store)
    assert (q["deposit_percent"], q.get("deposit_base")) == (20.0, None)       # total 2300 > 2000 wins
    q = db.create_quote("t1", "k@x.co", "K", [_ln("P", 300, True), _ln("L", 1000)], client=store)
    assert (q["deposit_percent"], q["deposit_base"]) == (50.0, 300.0)


def test_base_null_is_todays_amount_and_the_owner_can_change_the_percent():
    old = {"deposit_percent": 25, "subtotal": 200.0}
    assert quoting.deposit_net_amount(old) == 50.0
    assert quoting.deposit_net_amount({**old, "deposit_base": None}) == 50.0
    with_base = {"deposit_percent": 50, "subtotal": 1000.0, "deposit_base": 300.0}
    assert quoting.deposit_net_amount(with_base) == 150.0
    assert quoting.deposit_net_amount({**with_base, "deposit_percent": 20}) == 60.0    # base x the new percent
    assert quoting.deposit_net_amount({**with_base, "deposit_base": 0}) is None        # nothing ticked: none due
    snap = {**with_base, "acceptance_snapshot": {"subtotal": 1000.0, "deposit_base": 300.0}}
    assert quoting.deposit_net_amount(snap) == 150.0
    assert quoting.deposit_invoice_lines(with_base)[0]["amount"] == 150.0               # the invoice uses the same function


def test_the_flag_survives_create_edit_accept_and_the_base_follows_the_edit(monkeypatch):
    store = _rig(monkeypatch, RULE_T)
    q = db.create_quote("t1", "k@x.co", "K", [_ln("Parts", 300, True), _ln("Labour", 700)], client=store)
    assert q["deposit_base"] == 300.0
    # edit: the owner ticks a second line -> the base follows; the percent is left alone
    db.update_quote_lines(q["id"], "t1", [_ln("Parts", 300, True), _ln("More parts", 120, True), _ln("Labour", 700)],
                          client=store)
    row = store.s["quotes"][0]
    assert row["deposit_base"] == 420.0 and row["deposit_percent"] == 50.0 and row["subtotal"] == 1120.0
    assert [l.get("counts_to_deposit_rule", False) for l in db.list_quote_lines(q["id"], "t1", client=store)] \
        == [True, True, False]
    # edit again with nothing ticked: the base becomes 0 (no deposit due), the percent stays
    db.update_quote_lines(q["id"], "t1", [_ln("Parts", 300), _ln("Labour", 700)], client=store)
    assert store.s["quotes"][0]["deposit_base"] == 0.0 and store.s["quotes"][0]["deposit_percent"] == 50.0
    assert quoting.deposit_net_amount(store.s["quotes"][0]) is None
    db.update_quote_lines(q["id"], "t1", [_ln("Parts", 300, True), _ln("Labour", 700)], client=store)
    # accept: the snapshot freezes the base and each line's flag
    acc = db.accept_quote(q["id"], "t1", "Kris")
    snap = acc["acceptance_snapshot"]
    assert snap["deposit_base"] == 300.0
    assert [l.get("counts_to_deposit_rule", False) for l in snap["lines"]] == [True, False]
    assert quoting.deposit_net_amount(acc) == 150.0


def test_editing_a_quote_with_no_base_leaves_it_alone(monkeypatch):
    store = _rig(monkeypatch, {"default": 25})
    q = db.create_quote("t1", "k@x.co", "K", [{"description": "A", "amount": 100}], client=store)
    db.update_quote_lines(q["id"], "t1", [_ln("A", 100, True), _ln("B", 100)], client=store)
    assert "deposit_base" not in store.s["quotes"][0]
    assert store.s["quotes"][0]["deposit_percent"] == 25.0


def test_old_quotes_and_snapshots_without_the_flag_behave_as_before():
    snap = {"subtotal": 200.0, "lines": [{"description": "A", "amount": 200.0}]}
    q = {"deposit_percent": 25, "subtotal": 200.0, "acceptance_snapshot": snap, "quote_ref": "QUO-1"}
    assert quoting.deposit_net_amount(q) == 50.0
    assert quoting.ticked_net_sum([{"amount": 5, "is_selected": True}]) is None


def test_the_quote_pages_say_n_percent_of_the_base_only_when_there_is_one():
    root = Path(__file__).resolve().parent.parent / "src/app"
    for rel in ("templates/app/quote.html", "templates/quote_public.html"):
        t = (root / rel).read_text(encoding="utf-8")
        assert "deposit_base is not none" in t


def test_the_customers_own_untick_moves_the_base(monkeypatch):
    """Review of step 2: an optional ticked line the customer unticks on the
    quote page stops counting, so the page and the acceptance show what is due."""
    store = _rig(monkeypatch, RULE_T)
    q = db.create_quote("t1", "k@x.co", "K",
                        [_ln("Parts", 300, True), _ln("Extra parts", 120, True, is_optional=True), _ln("Labour", 700)],
                        client=store)
    assert q["deposit_base"] == 420.0
    store.s["quotes"][0]["status"] = "sent"
    extra = [l for l in store.s["quote_lines"] if l["description"] == "Extra parts"][0]
    assert db.set_quote_line_selected(q["id"], "t1", extra["id"], False) is True
    assert store.s["quotes"][0]["deposit_base"] == 300.0 and store.s["quotes"][0]["deposit_percent"] == 50.0
    acc = db.accept_quote(q["id"], "t1", "Kris")
    assert acc["acceptance_snapshot"]["deposit_base"] == 300.0
    assert quoting.deposit_net_amount(acc) == 150.0


def test_a_bulk_insert_of_lines_always_has_uniform_keys():
    """Live, 1 Oct 2026: the first quote mixing a ticked line with unticked ones
    returned a 500 and was left with no lines. The data API refuses a bulk
    insert whose rows have different keys; the fake store here does not, so
    this pins the row shapes themselves."""
    mixed = db._quote_line_rows("t1", "q1", [_ln("Parts", 280, True), _ln("Labour", 112.5), _ln("Extra", 18, is_optional=True)])
    assert len({tuple(sorted(r)) for r in mixed}) == 1
    assert [r["counts_to_deposit_rule"] for r in mixed] == [True, False, False]
    plain = db._quote_line_rows("t1", "q1", [_ln("A", 10), _ln("B", 20)])
    assert all("counts_to_deposit_rule" not in r for r in plain)          # an unticked quote writes what it always did
    assert len({tuple(sorted(r)) for r in plain}) == 1
