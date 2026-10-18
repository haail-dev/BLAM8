"""Widget fill fixes from the first walk (Garage walk, 1 Oct 2026).

Run: python -m pytest tests/test_walk_fixes_fill.py -q
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    a = JS.index(start)
    return JS[a:JS.index(end, a)]


def test_pffillrows_accepts_array_entries():
    """Garage walk, 1 Oct 2026 (F14): pfFillRows takes an array of parts."""
    body = _between("function pfFillRows(", "function pfBanner(")
    assert "Array.isArray(value)" in body
    assert 'value.split("|")' in body  # string callers unchanged


def test_booking_details_passes_named_fields_without_reparsing():
    body = _between('data.page === "booking_details"', 'data.page === "crm_fields"')
    assert 'pfFillNamedRows("rows", "row-template", values, "field"' in body
    assert 'services: "services", remember: "remember", kind: "doc"' in body
    assert '.split("|")' not in body


def test_dependants_fills_verified_type_and_options():
    body = _between('var byKind = {};', 'groupsEl.querySelectorAll(".dep-group").forEach(function (g, gi)')
    assert 'pfSetField(row, "f_type", f.type)' in body
    assert 'pfSetField(row, "f_options", (f.options || []).join(", "))' in body
    assert 'groupsEl.firstElementChild' not in body


def test_unknown_type_stops_the_whole_fill_before_any_row_changes():
    body = _between('async function applyPrefillData(data)', 'function pfFillNamedRows(')
    assert 'if (!checked.ok) throw new Error' in body
    assert body.index('if (!checked.ok)') < body.index('applyValidatedPrefillData(Object.assign')
