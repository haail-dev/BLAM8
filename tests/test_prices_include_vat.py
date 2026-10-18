"""Prices that already include VAT: one setting (extras.prices_include_vat,
True / False / unset), ONE function (invoice_config.split_vat) that turns line
amounts into (subtotal, vat, total), and a document that remembers its own basis.

Unset behaves exactly as before (VAT added on top). Under "included" the lines are
the prices the customer was told: the total equals their sum, VAT is carved out of
it, and deposits and balances are taken and netted off in that same line basis.

Run: python -m pytest tests/test_prices_include_vat.py -q
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, invoice_config, invoice_pdf, invoicing, payments, quote_pdf, quote_public, quoting
from app.invoice_config import split_vat
from tests.test_deposit_by_value import _Store

ROOT = Path(__file__).resolve().parent.parent


# ── split_vat ─────────────────────────────────────────────────────────────────
S20 = {"show_vat": True, "vat_rate": 20}
INC = {"show_vat": True, "vat_rate": 20, "prices_include_vat": True}


def test_split_vat_added_on_top():
    assert split_vat(45, S20) == (45.0, 9.0, 54.0)
    assert split_vat(165, S20) == (165.0, 33.0, 198.0)
    assert split_vat(100.01, S20) == (100.01, 20.0, 120.01)        # round(20.002, 2)
    assert split_vat(0, S20) == (0.0, 0.0, 0.0)


def test_split_vat_included():
    assert split_vat(45, INC) == (37.5, 7.5, 45.0)
    assert split_vat(165, INC) == (137.5, 27.5, 165.0)
    # 100.01 at 20%: vat = 100.01 x 20 / 120 = 16.6683 -> 16.67; subtotal = 83.34
    assert split_vat(100.01, INC) == (83.34, 16.67, 100.01)
    assert split_vat(0, INC) == (0.0, 0.0, 0.0)


def test_split_vat_included_total_always_equals_the_lines():
    for amount in (0.01, 1.0, 9.99, 33.33, 99.99, 100.01, 123.45, 1999.99, 12345.67):
        sub, vat, total = split_vat(amount, INC)
        assert total == round(amount, 2) and round(sub + vat, 2) == total


def test_split_vat_half_pennies_round_half_up_not_by_float_luck():
    # 0.15 x 20 / 120 = 0.025 exactly -> 0.03 (a float would give 0.02 or 0.03 by luck)
    assert split_vat(0.15, INC)[1] == 0.03
    assert split_vat(0.45, INC)[1] == 0.08         # 0.075


def test_split_vat_other_rates():
    assert split_vat(105, {"show_vat": True, "vat_rate": 5, "prices_include_vat": True}) == (100.0, 5.0, 105.0)
    assert split_vat(100, {"show_vat": True, "vat_rate": 5}) == (100.0, 5.0, 105.0)


def test_split_vat_rate_zero_and_vat_off_are_the_same_on_either_basis():
    for cfg in ({"show_vat": True, "vat_rate": 0}, {"show_vat": False, "vat_rate": 20}, {}, None):
        for inc in (None, True, False):
            assert split_vat(45, cfg, include=inc) == (45.0, 0.0, 45.0)
        assert split_vat(45, {**(cfg or {}), "prices_include_vat": True}) == (45.0, 0.0, 45.0)


def test_split_vat_include_overrides_the_settings_basis():
    assert split_vat(45, INC, include=False) == (45.0, 9.0, 54.0)       # a document with its own basis: added
    assert split_vat(45, S20, include=True) == (37.5, 7.5, 45.0)
    assert split_vat(45, INC, include=None) == (37.5, 7.5, 45.0)        # None = the settings'
    assert split_vat(45, {**S20, "prices_include_vat": False}, include=None) == (45.0, 9.0, 54.0)


# ── the setting: three states through the form ────────────────────────────────
def _round_trip(form):
    out = invoice_config.clean_form(form)
    return invoice_config.settings_for({"extras": out.get("extras", {})})["prices_include_vat"]


def test_settings_form_round_trip_all_three_states():
    assert _round_trip({"prices_include_vat_form": "1", "prices_include_vat": "1"}) is True
    assert _round_trip({"prices_include_vat_form": "1", "prices_include_vat": "0"}) is False
    assert _round_trip({"prices_include_vat_form": "1"}) is None                # neither chosen
    assert _round_trip({"prices_include_vat_form": "1", "prices_include_vat": "maybe"}) is None
    # a form that never showed it (another section's save) leaves it alone
    assert "prices_include_vat" not in invoice_config.clean_form({}).get("extras", {})
    assert "prices_include_vat" not in invoice_config.clean_form({"show_vat": "1"}).get("extras", {})
    # unset is written as null, so it clears an earlier choice when the page's save merges extras
    cleared = invoice_config.clean_form({"prices_include_vat_form": "1"})["extras"]
    assert cleared["prices_include_vat"] is None
    assert {**{"prices_include_vat": True}, **cleared}["prices_include_vat"] is None
    assert invoice_config.settings_for(None)["prices_include_vat"] is None
    assert invoice_config.settings_for({"extras": {"prices_include_vat": "yes"}})["prices_include_vat"] is None


def test_settings_page_shows_the_choice_only_with_vat_on_and_neither_selected_when_unset():
    t = (ROOT / "src/app/templates/app/invoice_settings.html").read_text(encoding="utf-8")
    assert "My prices already include VAT" in t and "VAT is added to my prices" in t
    assert "Not chosen yet: VAT is added on top until you choose." in t
    from app.templating import templates
    base = {"s": invoice_config.settings_for({"show_vat": True}), "fold": "", "foldbody": "", "inp": ""}
    # render just the fragment: the fieldset, via a tiny template from the real source
    start = t.index('<fieldset id="vat-basis"')
    frag = t[start:t.index("</fieldset>", start) + len("</fieldset>")]
    env = templates.env

    def render(raw):
        return env.from_string(frag).render(s=invoice_config.settings_for(raw))
    unset = render({"show_vat": True})
    assert "hidden" not in unset.split(">")[0]                       # VAT on: shown
    assert "checked" not in unset and "Not chosen yet" in unset      # neither selected
    assert "hidden" in render({"show_vat": False}).split(">")[0]     # VAT off: not shown
    inc = render({"show_vat": True, "extras": {"prices_include_vat": True}})
    assert inc.count("checked") == 1 and 'value="1" checked' in inc and "Not chosen yet" not in inc
    added = render({"show_vat": True, "extras": {"prices_include_vat": False}})
    assert added.count("checked") == 1 and 'value="0" checked' in added and "Not chosen yet" not in added


# ── an in-memory store with invoices too ──────────────────────────────────────
class _Inv(_Store):
    def __init__(self):
        super().__init__()
        self.s.update({"invoices": [], "invoice_lines": []})


def _rig(monkeypatch, *, vat=True, include=None, extras=None, default=None):
    """include: True / False / None = the setting. Returns (store, raw settings row)."""
    store = _Inv()
    ex = dict(extras or {})
    if include is not None:
        ex["prices_include_vat"] = include
    raw = {"extras": ex, "default_deposit_percent": default, "show_vat": vat, "vat_rate": 20}
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: raw)
    monkeypatch.setattr(db, "next_quote_ref", lambda t, client=None: "QUO-1001")
    monkeypatch.setattr(db, "next_invoice_ref", lambda t, client=None: "INV-1001")
    monkeypatch.setattr(db, "deal_id_for_job", lambda *a, **k: None)
    monkeypatch.setattr(db, "open_deal_id_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "db", lambda: store)
    return store, raw


def _l(desc, amount, **kw):
    return {"description": desc, "amount": amount, **kw}


def _quote(store, lines, **kw):
    return db.create_quote("t1", "k@x.co", "K", lines, client=store, **kw)


# ── unset == today ────────────────────────────────────────────────────────────
def test_unset_a_quote_is_todays_numbers_and_writes_no_new_key(monkeypatch):
    store, _ = _rig(monkeypatch)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    assert (q["subtotal"], q["vat"], q["total"]) == (165.0, 33.0, 198.0)       # the bug's numbers, unchanged
    assert "prices_include_vat" not in q and "prices_include_vat" not in store.s["quotes"][0]
    acc = db.accept_quote(q["id"], "t1", "Kris")
    assert "prices_include_vat" not in acc["acceptance_snapshot"]


def test_false_stored_is_also_added_on_top_and_writes_no_key(monkeypatch):
    store, _ = _rig(monkeypatch, include=False)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    assert (q["subtotal"], q["vat"], q["total"]) == (165.0, 33.0, 198.0)
    assert "prices_include_vat" not in store.s["quotes"][0]


def test_unset_an_invoice_is_todays_numbers_and_writes_no_new_key(monkeypatch):
    store, _ = _rig(monkeypatch)
    inv = db.create_invoice("t1", "k@x.co", "K", None, None, [_l("A", 120), _l("B", 45)], client=store)
    assert (inv["subtotal"], inv["vat"], inv["total"]) == (165.0, 33.0, 198.0)
    assert "prices_include_vat" not in inv
    db.update_invoice_lines(inv["id"], "t1", [_l("A", 100)], client=store)
    row = store.s["invoices"][0]
    assert (row["subtotal"], row["vat"], row["total"]) == (100.0, 20.0, 120.0)


def test_unset_vat_off_is_todays_numbers(monkeypatch):
    store, _ = _rig(monkeypatch, vat=False)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    assert (q["subtotal"], q["vat"], q["total"]) == (165.0, 0.0, 165.0)


def test_unset_a_preview_built_from_bookings_is_todays_numbers(monkeypatch):
    bookings = [{"id": "b1", "booking_ref": "BK-1", "summary": "Job", "contact": "k@x.co", "engineer_id": "e",
                 "quoted_amount": 120.0, "booking_date": "2026-10-01"},
                {"id": "b2", "booking_ref": "BK-2", "summary": "Job", "contact": "k@x.co", "engineer_id": "e",
                 "quoted_amount": 45.0, "booking_date": "2026-10-02"}]
    d = invoicing.fsm_drafts(bookings, None, vat_rate=0.20)[0]
    assert (d["subtotal"], d["vat"], d["total"]) == (165.0, 33.0, 198.0)
    d = invoicing.fsm_drafts(bookings, None)[0]                                  # the module default 20%
    assert (d["subtotal"], d["vat"], d["total"]) == (165.0, 33.0, 198.0)


def test_unset_deposit_and_balance_are_todays_numbers(monkeypatch):
    store, _ = _rig(monkeypatch, default=50)
    q = _quote(store, [_l("A", 280)])
    assert q["deposit_percent"] == 50.0 and (q["subtotal"], q["total"]) == (280.0, 336.0)
    assert quoting.deposit_net_amount(q) == 140.0                                  # of the subtotal
    dep = db.create_invoice("t1", "k@x.co", "K", None, None, quoting.deposit_invoice_lines(q), client=store)
    assert (dep["subtotal"], dep["vat"], dep["total"]) == (140.0, 28.0, 168.0)     # VAT added on top
    acc = db.accept_quote(q["id"], "t1", "Kris")
    lines = quoting.conversion_lines(acc, {**dep, "status": "paid"})
    assert [l["amount"] for l in lines] == [280.0, -140.0]
    bal = db.create_invoice("t1", "k@x.co", "K", None, None, lines, client=store)
    assert (bal["subtotal"], bal["vat"], bal["total"]) == (140.0, 28.0, 168.0)


# ── included ──────────────────────────────────────────────────────────────────
def test_included_quote_total_equals_the_lines(monkeypatch):
    store, _ = _rig(monkeypatch, include=True)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    assert q["total"] == 165.0 and (q["subtotal"], q["vat"]) == (137.5, 27.5)
    assert q["prices_include_vat"] is True


def test_included_optional_lines_count_only_when_selected(monkeypatch):
    store, _ = _rig(monkeypatch, include=True)
    q = _quote(store, [_l("A", 120), _l("Extra", 45, is_optional=True, is_selected=False)])
    assert q["total"] == 120.0
    store.s["quotes"][0]["status"] = "sent"
    extra = [l for l in store.s["quote_lines"] if l["description"] == "Extra"][0]
    assert db.set_quote_line_selected(q["id"], "t1", extra["id"], True) is True
    row = store.s["quotes"][0]
    assert (row["total"], row["vat"], row["subtotal"]) == (165.0, 27.5, 137.5)


def test_included_invoice_total_equals_the_lines_and_a_line_edit_keeps_the_basis(monkeypatch):
    store, raw = _rig(monkeypatch, include=True)
    inv = db.create_invoice("t1", "k@x.co", "K", None, None, [_l("A", 120), _l("B", 45)], client=store)
    assert (inv["subtotal"], inv["vat"], inv["total"]) == (137.5, 27.5, 165.0) and inv["prices_include_vat"] is True
    raw["extras"]["prices_include_vat"] = False                                     # the setting flips
    db.update_invoice_lines(inv["id"], "t1", [_l("A", 120), _l("B", 60)], client=store)
    row = store.s["invoices"][0]
    assert (row["subtotal"], row["vat"], row["total"]) == (150.0, 30.0, 180.0)      # still included


def test_included_preview_from_bookings_matches_what_an_invoice_will_bill(monkeypatch):
    bookings = [{"id": "b1", "booking_ref": "BK-1", "summary": "Job", "contact": "k@x.co", "engineer_id": "e",
                 "quoted_amount": 120.0, "booking_date": "2026-10-01"},
                {"id": "b2", "booking_ref": "BK-2", "summary": "Job", "contact": "k@x.co", "engineer_id": "e",
                 "quoted_amount": 45.0, "booking_date": "2026-10-02"}]
    d = invoicing.fsm_drafts(bookings, None, vat_rate=0.20, include=True)[0]
    assert (d["subtotal"], d["vat"], d["total"]) == (137.5, 27.5, 165.0)
    store, _ = _rig(monkeypatch, include=True)
    inv = db.create_invoice("t1", "k@x.co", "K", None, None, d["lines"], client=store)
    assert (inv["subtotal"], inv["vat"], inv["total"]) == (d["subtotal"], d["vat"], d["total"])
    # the view reads the settings: rate and basis
    monkeypatch.setattr(invoicing, "_preview_tax", lambda *a, **k: (0.20, True))
    monkeypatch.setattr(db, "list_completed_fsm_jobs", lambda *a, **k: bookings)
    monkeypatch.setattr(db, "get_fsm_rate_card", lambda *a, **k: None)
    monkeypatch.setattr(invoicing, "_unbilled", lambda t, rows, client=None: rows)
    monkeypatch.setattr(invoicing, "_apply_party_overrides", lambda t, rows, client=None: rows)
    assert invoicing.fsm_drafts_view("t1", "2026-10-01", "2026-10-31")[0]["total"] == 165.0
    monkeypatch.setattr(invoicing, "_preview_tax", lambda *a, **k: (0.20, False))
    assert invoicing.fsm_drafts_view("t1", "2026-10-01", "2026-10-31")[0]["total"] == 198.0


def test_included_flat_deposit_is_a_percent_of_the_lines_and_the_invoice_totals_it(monkeypatch):
    store, _ = _rig(monkeypatch, include=True, default=50)
    q = _quote(store, [_l("Parts", 280)])
    assert q["deposit_percent"] == 50.0 and q["total"] == 280.0
    assert quoting.deposit_net_amount(q) == 140.0                                   # of the TOTAL as written
    acc = db.accept_quote(q["id"], "t1", "Kris")
    assert acc["acceptance_snapshot"]["prices_include_vat"] is True
    assert quoting.deposit_net_amount(acc) == 140.0
    lines = quoting.deposit_invoice_lines(acc)
    assert lines[0]["amount"] == 140.0
    dep = db.create_invoice("t1", "k@x.co", "K", None, None, lines, client=store,
                            prices_include_vat=acc["prices_include_vat"] is True)
    assert dep["total"] == 140.0 and dep["vat"] == 23.33 and dep["subtotal"] == 116.67
    assert dep["prices_include_vat"] is True


def test_included_value_rule_is_tested_and_taken_on_the_total(monkeypatch):
    store, _ = _rig(monkeypatch, include=True,
                    extras={"deposit_rules": [{"over": 250, "percent": 50}]})
    q = _quote(store, [_l("A", 250)])                                  # 250: not over 250
    assert q["deposit_percent"] is None
    q = _quote(store, [_l("A", 250.01)])
    assert q["deposit_percent"] == 50.0 and quoting.deposit_net_amount(q) == 125.0   # 50% of 250.01 = 125.005 -> 125.0
    # "added" is tested on the gross (with VAT), as before: 220 net -> 264 > 250
    store, _ = _rig(monkeypatch, include=False, extras={"deposit_rules": [{"over": 250, "percent": 50}]})
    assert _quote(store, [_l("A", 220)])["deposit_percent"] == 50.0
    store, _ = _rig(monkeypatch, include=True, extras={"deposit_rules": [{"over": 250, "percent": 50}]})
    assert _quote(store, [_l("A", 220)])["deposit_percent"] is None  # 220 as written: no VAT added to it


RULE_T = {"deposit_rules": [{"over": 250, "percent": 50, "basis": "ticked"}]}


def test_included_ticked_rule_parts_280_labour_112_50_takes_140_of_the_ticked_sum(monkeypatch):
    store, _ = _rig(monkeypatch, include=True, extras=RULE_T)
    q = _quote(store, [_l("Parts", 280, counts_to_deposit_rule=True), _l("Labour", 112.50)])
    assert q["total"] == 392.5
    assert q["deposit_percent"] == 50.0 and q["deposit_base"] == 280.0
    assert quoting.deposit_net_amount(q) == 140.0
    # threshold on the ticked sum as written: 250 is not over 250, 250.01 is
    store, _ = _rig(monkeypatch, include=True, extras=RULE_T)
    assert _quote(store, [_l("P", 250, counts_to_deposit_rule=True)])["deposit_percent"] is None
    assert _quote(store, [_l("P", 220, counts_to_deposit_rule=True)])["deposit_percent"] is None  # 264 gross would match "added"
    # and under "added" the same 220 matches (264 > 250), unchanged
    store, _ = _rig(monkeypatch, include=False, extras=RULE_T)
    assert _quote(store, [_l("P", 220, counts_to_deposit_rule=True)])["deposit_percent"] == 50.0


def test_included_balance_after_the_deposit_nets_off_in_the_same_basis(monkeypatch):
    store, _ = _rig(monkeypatch, include=True, extras=RULE_T)
    q = _quote(store, [_l("Parts", 280, counts_to_deposit_rule=True), _l("Labour", 112.50)])
    acc = db.accept_quote(q["id"], "t1", "Kris")
    dep = db.create_invoice("t1", "k@x.co", "K", None, None, quoting.deposit_invoice_lines(acc), client=store,
                            prices_include_vat=True)
    assert dep["total"] == 140.0
    assert quoting.amount_as_written(dep) == 140.0 and quoting.amount_as_written({**dep, "prices_include_vat": None}) == dep["subtotal"]
    lines = quoting.conversion_lines(acc, {**dep, "status": "paid"})
    assert [l["amount"] for l in lines] == [280.0, 112.5, -140.0]
    bal = db.create_invoice("t1", "k@x.co", "K", None, None, lines, client=store,
                            prices_include_vat=acc["prices_include_vat"] is True)
    assert bal["total"] == 252.5 and bal["prices_include_vat"] is True               # 392.50 - 140.00
    assert round(bal["total"] + dep["total"], 2) == q["total"]
    # an unpaid deposit reads "invoiced", same amount
    assert "invoiced" in quoting.conversion_lines(acc, {**dep, "status": "sent"})[-1]["description"]


def test_a_converted_invoice_and_the_deposit_invoice_inherit_the_quotes_basis_not_the_settings(monkeypatch):
    store, raw = _rig(monkeypatch, include=True, default=50)
    q = _quote(store, [_l("A", 280)])
    raw["extras"]["prices_include_vat"] = False                                       # the setting flips after
    acc = db.accept_quote(q["id"], "t1", "Kris")
    assert acc["acceptance_snapshot"]["prices_include_vat"] is True
    # mint_deposit_invoice (the real function) with the invoice store behind it
    monkeypatch.setattr(db, "set_invoice_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "link_quote_invoice", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_invoice_payment_link", lambda *a, **k: None)
    monkeypatch.setattr(payments, "payment_url", lambda i: "https://pay/" + i)
    real_create = db.create_invoice
    monkeypatch.setattr(db, "create_invoice", lambda *a, **k: real_create(*a, client=store, **k))
    assert quote_public.mint_deposit_invoice("t1", acc).startswith("https://pay/")
    dep = store.s["invoices"][0]
    assert dep["total"] == 140.0 and dep["prices_include_vat"] is True


def test_a_quote_made_before_the_setting_keeps_added_even_if_the_setting_flips_on(monkeypatch):
    store, raw = _rig(monkeypatch)                                                    # unset
    q = _quote(store, [_l("A", 100)])
    assert (q["subtotal"], q["vat"], q["total"]) == (100.0, 20.0, 120.0)
    raw["extras"]["prices_include_vat"] = True                                        # chosen afterwards
    db.update_quote_lines(q["id"], "t1", [_l("A", 100), _l("B", 50)], client=store)
    row = store.s["quotes"][0]
    assert (row["subtotal"], row["vat"], row["total"]) == (150.0, 30.0, 180.0)        # still added on top
    store.s["quotes"][0]["status"] = "sent"
    store.s["quote_lines"][:] = []
    db.update_quote_lines(q["id"], "t1", [_l("A", 100), _l("X", 10, is_optional=True, is_selected=False)], client=store)
    x = [l for l in store.s["quote_lines"] if l["description"] == "X"][0]
    db.set_quote_line_selected(q["id"], "t1", x["id"], True)
    assert store.s["quotes"][0]["total"] == 132.0                                     # 110 + 22
    # and an invoice from that quote stays added too
    acc = db.accept_quote(q["id"], "t1", "Kris")
    assert "prices_include_vat" not in acc["acceptance_snapshot"]
    inv = db.create_invoice("t1", "k@x.co", "K", None, None, quoting.conversion_lines(acc),
                            client=store, prices_include_vat=acc.get("prices_include_vat") is True)
    assert inv["total"] == 132.0 and "prices_include_vat" not in inv


def test_an_included_quote_keeps_its_totals_after_the_setting_flips_and_after_a_line_edit(monkeypatch):
    store, raw = _rig(monkeypatch, include=True)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    raw["extras"]["prices_include_vat"] = False
    db.update_quote_lines(q["id"], "t1", [_l("A", 120), _l("B", 45)], client=store)
    row = store.s["quotes"][0]
    assert (row["subtotal"], row["vat"], row["total"]) == (137.5, 27.5, 165.0)
    db.update_quote_lines(q["id"], "t1", [_l("A", 120), _l("B", 80)], client=store)
    assert store.s["quotes"][0]["total"] == 200.0 and store.s["quotes"][0]["vat"] == 33.33
    raw["show_vat"] = False                                                           # VAT off: nothing to carve out
    db.update_quote_lines(q["id"], "t1", [_l("A", 120)], client=store)
    assert (store.s["quotes"][0]["subtotal"], store.s["quotes"][0]["vat"], store.s["quotes"][0]["total"]) == (120.0, 0.0, 120.0)


def test_vat_off_included_is_the_plain_sum_and_stores_the_basis(monkeypatch):
    store, _ = _rig(monkeypatch, vat=False, include=True)
    q = _quote(store, [_l("A", 120), _l("B", 45)])
    assert (q["subtotal"], q["vat"], q["total"]) == (165.0, 0.0, 165.0)


def test_booking_priced_by_an_accepted_included_quote_carries_the_stated_price(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "set_booking_quote", lambda bid, tid, amt, **k: seen.update(b=bid, amt=amt))
    snap = {"subtotal": 137.5, "vat": 27.5, "total": 165.0, "prices_include_vat": True, "lines": []}
    quote_public.accepted_side_effects("t1", {"id": "q1", "booking_id": "bk", "quote_ref": "QUO-1", "total": 165.0,
                                              "prices_include_vat": True, "acceptance_snapshot": snap})
    assert seen == {"b": "bk", "amt": 165.0}
    seen.clear()
    snap = {"subtotal": 100.0, "vat": 20.0, "total": 120.0, "lines": []}
    quote_public.accepted_side_effects("t1", {"id": "q1", "booking_id": "bk", "quote_ref": "QUO-1", "total": 120.0,
                                              "acceptance_snapshot": snap})
    assert seen == {"b": "bk", "amt": 100.0}


# ── display ───────────────────────────────────────────────────────────────────
def _card(inv, s):
    from app.templating import templates
    macro = templates.env.get_template("app/_invoice_sections.html").module.invoice_card
    return str(macro(inv, [{"description": "A", "amount": 165.0}], s, False, ""))


INV_ADDED = {"id": "i", "invoice_ref": "INV-1", "status": "sent", "customer_name": "K", "issue_date": "2026-10-01", "subtotal": 165.0, "vat": 33.0, "total": 198.0}
INV_INC = {"id": "i", "invoice_ref": "INV-1", "status": "sent", "customer_name": "K", "issue_date": "2026-10-01", "subtotal": 137.5, "vat": 27.5, "total": 165.0,
           "prices_include_vat": True}


def test_invoice_page_says_includes_vat_only_for_an_included_document():
    s = invoice_config.settings_for({"show_vat": True})
    inc = _card(INV_INC, s)
    assert "Total £165.00" in inc and "(includes VAT £27.50)" in inc
    assert "Subtotal" not in inc and "VAT (20%)" not in inc
    added = _card(INV_ADDED, s)
    assert "includes VAT" not in added and "Subtotal" in added and "VAT (20%)" in added
    # an included document with VAT now switched off shows the plain ladder
    off = _card(INV_INC, invoice_config.settings_for({"show_vat": False}))
    assert "includes VAT" not in off and "Subtotal" in off
    # the basis is the DOCUMENT's: the setting's own flag does not change how an old invoice reads
    flipped = invoice_config.settings_for({"show_vat": True, "extras": {"prices_include_vat": True}})
    assert "includes VAT" not in _card(INV_ADDED, flipped)


def _pay_html(inv, s):
    return payments._pay_page_html(inv, s, paid=False, bank=None, pay_now=False,
                                   lines=[{"description": "A", "amount": 165.0}])


def test_pay_page_says_includes_vat_only_for_an_included_document():
    s = invoice_config.settings_for({"show_vat": True})
    inc = _pay_html(INV_INC, s)
    assert "(includes VAT £27.50)" in inc and "£165.00" in inc and "Subtotal" not in inc
    added = _pay_html(INV_ADDED, s)
    assert "includes VAT" not in added and "Subtotal" in added


class _Rec:
    """Records the text of every cell fpdf is asked to draw."""
    texts: list


def _pdf_texts(monkeypatch, fn, *args):
    import fpdf
    texts = []
    real = fpdf.FPDF.cell

    def cell(self, w=None, h=None, text="", *a, **k):
        texts.append(str(text))
        return real(self, w, h, text, *a, **k)
    monkeypatch.setattr(fpdf.FPDF, "cell", cell)
    assert fn(*args)[:4] == b"%PDF"
    return texts


def test_pdfs_say_includes_vat_only_for_an_included_document(monkeypatch):
    s = invoice_config.settings_for({"show_vat": True})
    lines = [{"description": "A", "amount": 165.0, "date": "2026-10-01"}]
    inc = _pdf_texts(monkeypatch, invoice_pdf.render, INV_INC, lines, s)
    assert "Total (includes VAT £27.50)" in inc and "Subtotal" not in inc
    added = _pdf_texts(monkeypatch, invoice_pdf.render, INV_ADDED, lines, s)
    assert "Subtotal" in added and not any("includes VAT" in t for t in added)
    q_inc = {"quote_ref": "QUO-1", "subtotal": 137.5, "vat": 27.5, "total": 165.0, "prices_include_vat": True,
             "show_item_prices": True}
    qt = _pdf_texts(monkeypatch, quote_pdf.render, q_inc, lines, s)
    assert "Total (includes VAT £27.50)" in qt and "Subtotal" not in qt
    q_add = {"quote_ref": "QUO-1", "subtotal": 165.0, "vat": 33.0, "total": 198.0, "show_item_prices": True}
    qt = _pdf_texts(monkeypatch, quote_pdf.render, q_add, lines, s)
    assert "Subtotal" in qt and not any("includes VAT" in t for t in qt)


def test_quote_pages_say_includes_vat_only_for_an_included_quote():
    from app.templating import templates
    s = invoice_config.settings_for({"show_vat": True})
    base = {"quote_ref": "QUO-1", "status": "sent", "show_item_prices": True, "customer_name": "K"}
    lines = [{"id": "l1", "description": "A", "amount": 165.0, "is_optional": False, "is_selected": True}]

    def public(q, totals):
        return templates.get_template("quote_public.html").render(
            q=q, lines=lines, totals=totals, s=s, state="sent", msg=None, deposit_net=None, deposit_inv=None)

    inc = public({**base, "prices_include_vat": True}, {"subtotal": 137.5, "vat": 27.5, "total": 165.0})
    assert "£165.00" in inc and "(includes VAT £27.50)" in inc and "Subtotal" not in inc
    added = public(base, {"subtotal": 165.0, "vat": 33.0, "total": 198.0})
    assert "includes VAT" not in added and "Subtotal" in added and "VAT (20%)" in added
    # prices hidden: just the total, as before
    hidden = public({**base, "prices_include_vat": True, "show_item_prices": False},
                    {"subtotal": 137.5, "vat": 27.5, "total": 165.0})
    assert "includes VAT" not in hidden and "Subtotal" not in hidden
    # the owner's quote page carries the same condition on the same fields
    t = (ROOT / "src/app/templates/app/quote.html").read_text(encoding="utf-8")
    assert "q.prices_include_vat and s.show_vat" in t and "(includes VAT £" in t


def test_sample_invoice_previews_the_chosen_basis():
    inv, _ = invoice_config.sample_invoice(invoice_config.settings_for({"show_vat": True, "extras": {"prices_include_vat": True}}))
    assert inv["total"] == 145.0 and inv["vat"] == 24.17 and inv["prices_include_vat"] is True
    inv, _ = invoice_config.sample_invoice(invoice_config.settings_for({"show_vat": True}))
    assert inv["total"] == 174.0 and "prices_include_vat" not in inv


# ── no arithmetic left behind ─────────────────────────────────────────────────
def test_no_subtotal_times_rate_arithmetic_remains_outside_split_vat():
    import re
    pat = re.compile(r"(subtotal|ticked_net|net)\s*\*\s*(rate|vat_rate|invoice_config\.effective_vat_rate)")
    hits = []
    for p in (ROOT / "src/app").rglob("*.py"):
        if p.name == "pricing.py" or p.name == "fsm_pricing.py":     # rate-card maths, not a document's VAT
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert hits == []


def test_the_setup_assistant_can_set_the_basis_only_from_the_owners_answer():
    """Round three, 1 Oct 2026: the assistant said "prices include VAT" and no
    setting backed it. The catalogue now has the field, with its question."""
    from pathlib import Path
    from app import settings_recommend as sr
    desc = sr.PAGES["invoice_settings"]["fields"]["prices_include_vat"]
    assert "already include VAT" in desc and "only from what the owner says" in desc
    assert "is VAT already in it?" in desc
    assert sr.clean("invoice_settings", [{"name": "prices_include_vat", "value": "1"}]) == [{"name": "prices_include_vat", "value": "1"}]
    assert sr.display("invoice_settings", "prices_include_vat", "1") == "Prices include VAT: yes"
    assert sr.display("invoice_settings", "prices_include_vat", "0") == "Prices include VAT: no"
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    assert 'if (el.type === "radio") {' in js and "var on = r.value === value;" in js
