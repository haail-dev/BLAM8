"""The save bar appears when there is something to save — everywhere.

Kris, 2026-09-01: "the save buttons are still always showing, not just when
info is there to be save. Makes the app look dated."

Two failures sat behind that. The behaviour existed on two pages and nowhere
else, and on the pages that had it the most visible bar was never actually
hidden: `hidden` was set on the element, and Tailwind's `.flex` beat it. These
tests hold both halves down.
"""
import io
import re
import glob
import os

import pytest

from tests.test_settings_fold import client, _env      # noqa: F401  (fixture)


BASE = "src/app/templates/app/base.html"
SCRIPT = "src/app/static/dirty_save.js"


def _read(p):
    return io.open(p, encoding="utf-8").read()


# ── The bug that shipped: hidden that did not hide ─────────────────────────
def test_hiding_a_save_row_outranks_the_display_utility_on_it():
    """Tailwind preflight ships

        [hidden]:where(:not([hidden=until-found])){display:none}

    :where() adds nothing, so that selector weighs the same as ONE CLASS and is
    emitted before the utilities. A save row is a button plus a line of text, so
    it almost always carries .flex — which then wins, and `hidden` set by script
    leaves the bar sitting there in full view. It was verified once by reading
    element.hidden, which is true either way; the screen disagreed.

    The app-wide rule has to beat a utility, so it says so.
    """
    css = _read(BASE)
    rule = re.search(r"\[data-save-row\]\[hidden\]\s*\{([^}]*)\}", css)
    assert rule, "no rule hiding a marked save row"
    body = rule.group(1)
    assert "display" in body and "none" in body
    assert "!important" in body, (
        "without !important a save row carrying .flex stays on screen — "
        "equal specificity, and the utilities are emitted later"
    )


def test_the_rule_is_plain_css_in_the_template_not_a_tailwind_class():
    """tw.css is gitignored and built at deploy. A hiding rule that only exists
    after a successful Tailwind build is a rule that can go missing; this one
    ships inside the served HTML."""
    assert "[data-save-row][hidden]" in _read(BASE)
    tw = "src/app/static/tw.css"
    if os.path.exists(tw):
        assert "[data-save-row]" not in _read(tw), "don't depend on the build for this"


# ── One behaviour, not a copy per page ─────────────────────────────────────
def test_every_page_gets_the_shared_script():
    assert "/static/dirty_save.js" in _read(BASE)


def test_no_page_carries_its_own_copy_of_the_logic():
    """It was inline in two templates and started drifting immediately — one
    grew a MutationObserver, the other did not. One file now."""
    for p in glob.glob("src/app/templates/**/*.html", recursive=True):
        assert "function signature(form)" not in _read(p), f"{p} has its own copy"


def test_the_script_re_reads_the_form_rather_than_tracking_fields():
    """Comparing the whole serialised form is what makes 'undo your edit and the
    bar goes away again' work, and what survives rows being added or removed at
    runtime. Per-field listeners would do neither."""
    js = _read(SCRIPT)
    assert "new FormData(form)" in js
    assert "MutationObserver" in js, "the intake checklist adds and removes rows"
    assert "v instanceof File" in js, "a file input has no comparable value"


# ── The sweep: no edit form left with a permanent Save ─────────────────────
SAVE_BUTTON = re.compile(r"<button[^>]*>\s*(?:Save|Update)\b[^<]*</button>", re.I)

# Buttons that must always be visible, and why. A form that starts EMPTY has
# nothing to compare against — hiding its button until you type would read as a
# broken page, so these are create forms, not edit forms.
ALWAYS_VISIBLE = {
    "_thread.html": "writing the missing fact — the box starts blank",
    "draft_review.html": "adding a new knowledge entry / a new never-do rule",
    "_concierge_history.html": "the brief's cog: a list that is only on screen while you're changing it (fetch, no form)",
}


def _save_buttons():
    for p in sorted(glob.glob("src/app/templates/app/*.html")):
        s = _read(p)
        for m in SAVE_BUTTON.finditer(s):
            yield p, s, m


def test_every_save_button_in_the_app_is_marked():
    """The guard for the next page somebody builds: a Save button that saves an
    existing record has to say which element disappears with it."""
    unmarked = []
    for p, s, m in _save_buttons():
        # the marker is on the button itself or on a wrapper just outside it
        window = s[max(0, m.start() - 220):m.end()]
        name = os.path.basename(p)
        if "data-save-row" not in window and name not in ALWAYS_VISIBLE:
            unmarked.append(f"{name}:{s.count(chr(10), 0, m.start()) + 1}")
    assert not unmarked, "Save buttons with no data-save-row: " + ", ".join(unmarked)


def test_the_exemptions_are_all_still_real():
    """An allowlist nobody re-checks becomes a hole. Every exempt file must
    still contain a Save button, or the entry should go."""
    for name in ALWAYS_VISIBLE:
        assert SAVE_BUTTON.search(_read("src/app/templates/app/" + name)), \
            f"{name} is exempt but has no Save button"


def test_every_marker_sits_inside_the_form_it_saves():
    """closest('form') is how the script finds what to compare. A marker outside
    one is silently inert — no error, just a bar that never hides."""
    stray = []
    for p in sorted(glob.glob("src/app/templates/app/*.html")):
        s = _read(p)
        if "data-save-row" not in s or p.endswith("base.html"):
            continue
        depth = 0
        for m in re.finditer(r"<form\b|</form>|data-save-row", s):
            t = m.group(0)
            if t == "<form":
                depth += 1
            elif t == "</form>":
                depth -= 1
            elif depth < 1:
                stray.append(f"{os.path.basename(p)}:{s.count(chr(10), 0, m.start()) + 1}")
    assert not stray, "markers outside any form: " + ", ".join(stray)


def test_a_create_form_keeps_its_button():
    """Only forms that load with values hide their button. 'Add a stage' starts
    empty and its Add button must always be there, or the page looks broken —
    and it sits directly beneath six editable stage rows that now go quiet."""
    s = _read("src/app/templates/app/crm_settings.html")
    add = re.search(r'action="/app/crm/settings/stages"(.*?)</form>', s, re.S)
    assert add, "the add-a-stage form moved"
    assert "data-save-row" not in add.group(1)


# ── Rendered, not just authored ────────────────────────────────────────────
def test_the_page_renders_marked_rows_that_ship_visible(client, monkeypatch):
    """Rendered visible, hidden by script: a script that fails to load leaves
    every save bar reachable rather than stranding a form nobody can submit.

    Settings is the page with a full test environment, and it is the one that
    shipped the bug. The rest are covered by the static sweep above, and were
    checked in the browser."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    rows = re.findall(r"<(?:div|button)[^>]*data-save-row[^>]*>", html)
    assert len(rows) >= 3, rows
    for row in rows:
        assert not re.search(r"(?<![-\w])hidden(?![-\w])", row), \
            "ships a save row already hidden"
    assert "/static/dirty_save.js" in html


# ── Dependants: the add control lives in the save row ──────────────────────
def _dependants_save_row(client, monkeypatch, groups):
    """The rendered Dependants page's save-row wrapper: its opening tag and body.

    The nested Save button carries data-save-row as well (the sweep above looks
    for it), so the wrapper is the <div>, not every marked element."""
    import app.dashboard as dashboard

    _env(monkeypatch, modules={"scheduling"})
    snapshot = {"page": "dependants", "config": {}, "groups": groups,
                "instances": [], "definitions": []}
    monkeypatch.setattr(dashboard.setup_forms, "load", lambda *a: snapshot)
    monkeypatch.setattr(dashboard.db, "list_requirement_groups",
                        lambda t, client=None: list(groups))
    html = client.get("/app/diary/dependants").text
    rows = re.findall(r"(<div[^>]*data-save-row[^>]*>)(.*?)</div>", html, re.S)
    assert len(rows) == 1, rows
    return rows[0]


def test_an_empty_dependants_page_keeps_its_add_control_reachable(client, monkeypatch):
    """A tenant with no kind yet has no field in the form to change, so the
    dirty-save script would hide a plain save row. The page's only add button is
    inside it, which left an owner no way to start the page by hand."""
    tag, body = _dependants_save_row(client, monkeypatch, groups=[])
    assert "data-save-always" in tag
    assert 'id="add-group"' in body


def test_a_dependants_page_with_a_kind_hides_its_save_row_until_edited(client, monkeypatch):
    """Once a kind exists the form has fields to change, so the row goes back to
    appearing only when the form differs from how it loaded."""
    kind = {"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "cardinality": "many",
            "fields": [{"label": "Breed", "type": "text", "required": False,
                        "durability": "once"}]}
    tag, body = _dependants_save_row(client, monkeypatch, groups=[kind])
    assert "data-save-always" not in tag
    assert 'id="add-group"' in body
