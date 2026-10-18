"""Blank line rows hide behind "+ Add a line" (src/app/static/spare_rows.js; Kris
2026-09-24 on QUO-1029: "no need to display them if they're empty")."""


def _read(p):
    return open(p, encoding="utf-8").read()


def test_every_line_form_hides_its_spares_behind_the_plus():
    for p in ("src/app/templates/app/quote.html", "src/app/templates/app/_invoice_sections.html",
              "src/app/templates/app/quote_new.html", "src/app/templates/app/invoice_new.html"):
        t = _read(p)
        assert "data-spare-row hidden" in t, p
        assert "data-add-row" in t and "+ Add a line" in t, p
        assert "data-filled-row" in t, p


def test_the_script_rides_every_page():
    assert '<script src="/static/spare_rows.js?v={{ asset_rev }}" defer></script>' in _read("src/app/templates/app/base.html")
    js = _read("src/app/static/spare_rows.js")
    assert "if (!form.querySelector('[data-filled-row]')) showNext(false);" in js   # an empty list starts with one row


def test_a_hidden_spare_row_stays_hidden_despite_its_flex_class():
    # The rows carry both [hidden] and .flex; Tailwind's .flex beat [hidden], so all six blank rows showed
    # on every quote and invoice (garage Short, 6 Oct 2026). The app's own rule must win.
    assert "[data-spare-row][hidden] { display: none !important; }" in _read("src/app/templates/app/base.html")
