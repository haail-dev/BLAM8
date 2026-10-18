"""The contact's pencil sits to the right of the name, on the same line.

Kris, 2026-09-21: "on the Contacts CRM theres an Edit pencil under the
Contact name, put that next to the name on the right side."

It was inline in the name's <p> — but a <details> lays out as a block
whatever display it is handed, so the pencil dropped to its own line under
the name. Now the name row IS the summary: a flex row, name first, pencil
last, and the editor still unfolds beneath.

Run: python -m pytest tests/test_contact_pencil.py -q
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "src/app/templates/app/crm_contact.html").read_text(encoding="utf-8")
START = PAGE.index('<details class="group/edit">')


def test_the_name_row_is_the_summary_and_the_pencil_is_its_last_item():
    row = PAGE[START:PAGE.index("</summary>", START)]
    assert '<summary class="flex items-center' in row
    name, pencil = row.index("{{ c.name or c.email }}"), row.index("&#9998;")
    assert name < pencil                                   # name first, pencil to its right
    assert "flex-none" in row[name:pencil]                 # the pencil never wraps under the name
    assert "text-2xl font-bold font-display text-navy" in row[:name]
    assert 'title="Edit name, email or phone"' in row


def test_the_editor_still_unfolds_beneath_it():
    body = PAGE[START:PAGE.index("</details>", START)]
    assert 'action="/app/crm/contacts/{{ c.id }}/edit"' in body
    assert body.index("</summary>") < body.index("<form")
    assert "inline-block" not in body                      # the layout that fell under the name
