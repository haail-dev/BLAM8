"""The settled-setting fold (Kris 2026-09-01, redesign slice 6).

Settings was 7,354px on a 375px screen — nine phone screens for thirteen
sections, every one explaining itself at full length whether or not the operator
had already decided it. Each section is now a <details> whose closed face is one
line: the name, and what it is currently set to.

Pins the three things that make that safe rather than merely shorter:
  * a section with a value collapses, and its VALUE is the closed face — no
    "Set ✓" chip repeating what the value already says;
  * a section that is genuinely blank opens itself and says so with the amber
    dot the rest of the app uses for "this wants you";
  * the inputs inside a folded section are still in the form — <details> hides,
    it does not disable — so a save can never blank a setting you folded away.

The last one is the whole risk of this design, so it is asserted on the markup
that carries it: no `disabled`, and every field still inside its form.

Run: python -m pytest tests/test_settings_fold.py -q
"""
from tests.booking_fixtures import carried_config
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


# A tenant who has answered everything — every section should be folded away.
SETTLED = {
    "agent_name": "the office team",
    "tone": "warm and professional",
    "sign_off": "Kind regards,\nThe team",
    "business_brief": "We fix boilers.",
    "do_list": "Confirm the postcode\nOffer a callback",
    "never_do": "Never quote a firm price",
    "escalation": "complaint, legal, refund",
    "custom_instructions": "Mon–Fri only.",
    "debrief_time": "08:15",
    # voice is an onboarding step now: left unfinished it opens itself, so a
    # tenant who has "answered everything" must have finished it too (09-01)
    "voice_profile_source": "auto_learned",
}


def _env(monkeypatch, cfg=None, connected=True, modules=()):
    cfg = SETTLED if cfg is None else cfg
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "google", "mailbox": "hello@acme.co.uk",
        "portal_slug": "acme"})
    monkeypatch.setattr(dashboard.db, "get_connection_status",
                        lambda *a, **k: {"sub_expires": None} if connected else None)
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: bool(connected))
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config({}))
    monkeypatch.setattr(dashboard.setup_forms, "load", lambda ctx, page: {
        "page": page, "config": dashboard.db.get_scheduling_config(ctx.tenant_id) or {}, "definitions": []})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.portal, "ensure_portal_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(dashboard.storage, "banner_public_url", lambda *a, **k: None)


def _faces(html):
    """{section name: (closed-face value, opened?)} for every folded section."""
    out = {}
    for block in re.findall(r"<details[^>]*>\s*<summary[^>]*data-fold=.*?</summary>",
                            html, re.S):
        if "data-group" in block:
            continue          # a sheet's heading, not a setting's face
        name = re.search(r'data-fold="([^"]*)"', block).group(1).replace("&amp;", "&")
        # Strip the markup and take what is left between the section's name and
        # the chevron: the amber rows nest a dot span inside the value, so
        # counting spans doesn't survive both shapes.
        text = unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))).strip()
        value = text.removeprefix(name).strip().rstrip("›").strip()
        opened = bool(re.match(r"<details[^>]*\sopen", block))
        out[name] = (value, opened)
    return out


# ── A settled section collapses, and its value is the face ──────────────────
def test_every_answered_section_folds_to_one_line(client, monkeypatch):
    _env(monkeypatch)
    faces = _faces(client.get("/app/settings").text)
    assert not any(open_ for _, open_ in faces.values()), \
        "a tenant who has answered everything should land on a page of closed rows"
    # The face reads back the actual answers, not a generic "configured" chip.
    assert faces["Identity & voice"][0] == "the office team · warm and professional · Kind regards, The team"
    assert faces["Rules & escalation"][0] == "2 always-do · 1 never-do · 3 escalation words · your own notes"
    assert faces["Customer Portal"][0] == "blam8.com/c/acme · invite only"
    # The face speaks for the whole section now (Kris 2026-09-02): what reaches
    # you, then the brief — a settled tenant sees both without opening it.
    assert faces["Notifications"][0] == "Everything, as it happens · brief 08:15"
    assert faces["Mailbox connection"][0] == "hello@acme.co.uk · auto-draft active"


def test_no_set_chip_repeats_what_the_value_already_says(client, monkeypatch):
    """The professionalise pass took the app away from filled status pills, and
    ten rows each wearing a green tick say less than ten values do."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert "Set ✓" not in html
    assert "bg-emerald-50 border border-emerald-200 px-2.5" not in html  # the old voice pill


# ── A blank section speaks up ───────────────────────────────────────────────
@pytest.mark.parametrize("blank, section, says", [
    ({}, "Identity & voice", "Nothing set"),
    ({"agent_name": "x"}, "Rules & escalation", "No rules yet"),
])
def test_a_blank_section_opens_itself_in_amber(client, monkeypatch, blank, section, says):
    _env(monkeypatch, cfg=blank)
    html = client.get("/app/settings").text
    value, opened = _faces(html)[section]
    assert opened, f"{section} is unset and must not hide behind a closed row"
    assert says in value
    assert 'data-needs' in html


def test_a_disconnected_mailbox_is_the_loudest_row_on_the_page(client, monkeypatch):
    """Nothing else on this page stops the product working outright."""
    _env(monkeypatch, connected=False)
    value, opened = _faces(client.get("/app/settings").text)["Mailbox connection"]
    assert opened
    assert "No mailbox connected" in value


def test_close_account_never_opens_itself(client, monkeypatch):
    """A DELETE box sitting open at the foot of every visit is an accident
    waiting for a thumb — it opens only when a failed confirm bounced you back."""
    _env(monkeypatch)
    assert _faces(client.get("/app/settings").text)["Close account"][1] is False
    assert _faces(client.get("/app/settings?err=confirm").text)["Close account"][1] is True


# ── The risk: a fold must never drop a field from its form ──────────────────
def test_folding_never_disables_an_input(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/settings").text
    controls = re.findall(r"<(?:input|textarea|select)\b[^>]*>", html)
    assert controls
    # "disabled" also shows up as Tailwind's disabled: variant, which is a class
    # name and harmless — only the bare attribute drops a field from a save.
    assert not [c for c in controls if re.search(r"\bdisabled\b(?!:)", c)]


def test_every_settings_field_is_still_inside_the_form(client, monkeypatch):
    """<details> hides, it does not disable — but only if the fold stayed inside
    the <form>. A stray </form> from a mis-nested fold would silently drop the
    fields after it from every save."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    body = html.split('action="/app/settings"', 1)[1].split("</form>", 1)[0]
    for field in dashboard.SETTINGS_FIELDS:
        assert f'name="{field}"' in body, f"{field} fell out of the settings form"


def test_the_page_that_shares_the_scheduling_partial_does_not_fold_it(client, monkeypatch):
    """/app/diary/settings IS that partial — folding it there would leave a page
    whose only content is one closed line."""
    _env(monkeypatch, modules=("scheduling",))
    assert "data-fold" in client.get("/app/settings").text
    assert "data-fold" not in client.get("/app/diary/settings").text


# ── Appearance is a two-way switch (Kris 2026-09-01) ────────────────────────
def test_the_theme_picker_offers_only_light_and_dark(client, monkeypatch):
    """"System" looked identical to one of the other two on any given screen,
    with nothing saying which or why, so it read as a duplicate button."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    # buttons only — the script mentions the attribute too, and matching that
    # would let a third button hide behind a passing test
    choices = re.findall(r'<button[^>]*data-theme-choice="([^"]*)"', html)
    assert choices == ["light", "dark"]
    assert ">System<" not in html


def test_appearance_is_not_folded_because_the_control_is_the_value(client, monkeypatch):
    """Kris 2026-09-01: "it's an unneccessary click to show the 2 buttons."

    Every other section folds because its body is a form you fill in and save.
    This one's body is two buttons that act on press — so the disclosure bought
    a tap and revealed nothing the row couldn't already show. The control sits
    in the row and IS the value."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    picker = html.index('id="theme-picker"')
    head = html[:picker]
    depth = head.count("<details") - head.count("</details>")
    assert depth == 0, "the theme picker is inside a fold again"
    assert 'data-fold="Appearance"' not in html
    # and both choices are on the row, not behind anything
    assert html.count("data-theme-choice=") >= 2


def test_a_stored_system_choice_is_migrated_not_dropped(client, monkeypatch):
    """The pre-paint script must resolve an existing 'system' against the device
    and write it back. Falling through to the 'light' default instead would flip
    every one of those people on a dark phone to light — the single thing they
    had actively chosen not to happen."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert "pref === 'system'" in html, "the migration branch is gone"
    assert "prefers-color-scheme: dark" in html
    assert "setItem('blam8-theme', pref)" in html, "the resolved choice must persist"


# ── The "Your agent" sheet (Kris 2026-09-01) ───────────────────────────────
def test_voice_and_the_settings_it_governs_are_one_sheet(client, monkeypatch):
    """Voice auto-learning, the paragraph explaining what the rest is for, and
    the settings that paragraph introduced were three loose things in a row:
    a card, an orphaned paragraph, then five more cards. Nothing said they
    belonged together. They are one ruled sheet now, on the Workspace pattern."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    sheet = html.split(">Your agent<", 1)[1].split("</section>", 1)[0]
    for row in ("Voice auto-learning", "Sending mode", "Identity &amp; voice",
                "Rules &amp; escalation"):
        assert f'data-fold="{row}"' in sheet, f"{row} fell out of the sheet"
    # the governing paragraph moved into the sheet's header, not left floating
    # (collapsed — the template wraps it across lines)
    flat = re.sub(r"\s+", " ", html)
    assert "take precedence over everything here" in flat
    assert "Everything below teaches the agent" not in flat


def test_the_sheet_does_not_nest_one_form_inside_another(client, monkeypatch):
    """Voice auto-learning carries its own forms (learn / apply / discard) and
    the settings form carries the rest. Grouping them visually must not nest
    them structurally — a nested <form> is invalid and the inner one silently
    stops submitting."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    sheet = html.split(">Your agent<", 1)[1].split("</section>", 1)[0]
    depth = maxdepth = 0
    for token in re.findall(r"<form|</form>", sheet):
        depth += 1 if token.startswith("<form") else -1
        maxdepth = max(maxdepth, depth)
    assert maxdepth <= 1, "a form ended up inside another form"


# ── The allowance links to the allowance, wherever it's quoted ─────────────
@pytest.mark.parametrize("subscribed", [True, False])
def test_every_quoted_allowance_links_to_the_usage_page(client, monkeypatch, subscribed):
    """+300 emails means nothing without the number it is added to. Both
    branches of the mailbox fold quote it, so both link it."""
    _env(monkeypatch)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: subscribed)
    html = client.get("/app/settings").text
    fold = html.split('data-fold="Mailbox connection"', 1)[1].split("</details>", 1)[0]
    assert 'href="/app/usage"' in fold, "the allowance is quoted with nowhere to check it"


def test_the_mailbox_fold_never_nests_one_link_inside_another(client, monkeypatch):
    """The add row used to be a single <a> wrapping the whole line. Putting the
    usage link inside it would have nested anchors — the browser closes the
    outer one early and the markup stops meaning what it says. Two labelled
    links side by side instead."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    depth = maxdepth = 0
    for token in re.findall(r"<a|</a>", html):
        depth += 1 if token.startswith("<a") else -1
        maxdepth = max(maxdepth, depth)
    assert maxdepth <= 1, "an anchor ended up inside another anchor"


def test_links_in_one_sentence_look_like_each_other(client, monkeypatch):
    """Kris 2026-09-01: the two links in the second-inbox sentence were styled
    differently — one bold brand blue, one grey with a hairline underline —
    which reads as inconsistency rather than hierarchy. Same sentence, same
    treatment, and it is the app's one inline-link idiom for both."""
    _env(monkeypatch)
    # the unsubscribed branch — where both links share one sentence
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    html = client.get("/app/settings").text
    fold = html.split('data-fold="Mailbox connection"', 1)[1].split("</details>", 1)[0]
    classes = re.findall(r'<a [^>]*href="(?:/billing/plans|/app/usage)"[^>]*class="([^"]*)"', fold)
    assert len(classes) == 2, classes
    assert classes[0] == classes[1] == "text-brand hover:underline", classes


# ── An open fold states in full what its face abbreviates ──────────────────
def test_the_open_mailbox_fold_states_the_address_in_full(client, monkeypatch):
    """Kris 2026-09-01: "when i click to expand, i still can't see full
    subheader on mobile". The face truncates the address on a phone, and the
    body had been stripped of it on the grounds that repeating it was
    duplication — so opening the row showed you everything except the fact you
    opened it for. break-all too: an address is unbreakable text and a long one
    would push the card past the edge of the screen."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    fold = html.split('data-fold="Mailbox connection"', 1)[1].split("</details>", 1)[0]
    body = fold.split("</summary>", 1)[1]
    assert "hello@acme.co.uk" in body, "the address is only in the face, which truncates"
    assert "break-all" in body


def test_the_explainer_only_speaks_before_there_is_a_mailbox(client, monkeypatch):
    """"The inbox BLAM8 monitors, drafts for, and sends from" defines a thing
    you are already looking at once the address is on screen. Before you have
    connected one it is the whole point."""
    _env(monkeypatch, connected=True)
    assert "monitors, drafts for" not in client.get("/app/settings").text
    _env(monkeypatch, connected=False)
    assert "monitors, drafts for" in client.get("/app/settings").text


def test_an_open_face_stops_repeating_what_the_body_now_says(client, monkeypatch):
    """Kris 2026-09-01: "when expanded, stop showing subheader in header". Once
    the body states the value in full, the face's truncated copy of it a few
    pixels above is noise. `invisible` and not `hidden`, so the flex box stays
    and the chevron doesn't jump when the row opens — measured at 0px movement —
    and visibility:hidden also keeps it from being announced twice."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    faces = [f for f in re.findall(r'<summary[^>]*data-fold=.*?</summary>', html, re.S)
             if "data-group" not in f]      # group headings are not value faces
    assert faces
    for f in faces:
        name = re.search(r'data-fold="([^"]*)"', f).group(1)
        # the value span — the one carrying flex-1 — must go quiet on open.
        # (Appearance's carries an id before its class, so don't assume order.)
        value = re.search(r'<span [^>]*class="(flex-1[^"]*)"', f)
        assert value, f"{name} has no value span"
        # NAMED variant: an unnamed group-open matches any open .group
        # ancestor, so a nested row went blank whenever its SHEET opened
        assert "group-open/row:invisible" in value.group(1), (
            f"{name} keeps its face open")


def test_save_rows_ship_visible_so_a_broken_script_cannot_strand_a_save(client, monkeypatch):
    """Kris 2026-09-01: Save only shows once there is something to save. The
    hiding is done BY the script, not undone by it — rendered hidden and
    revealed with JS, a script that never runs would leave someone unable to
    save at all. This way the worst case is yesterday's behaviour."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    rows = re.findall(r"<div[^>]*data-save-row[^>]*>", html)
    # settings, portal, Concierge task timings, notifications+brief, Today's order
    assert len(rows) == 5, rows
    for row in rows:
        assert "hidden" not in row, "a save row would be invisible without JS"
    # and the script that does the hiding is actually on the page (it lives in
    # src/app/static/dirty_save.js now — one behaviour for every save bar in the app)
    assert "/static/dirty_save.js" in html


# ── The reorganised page (Kris 2026-09-01) ─────────────────────────────────
def test_the_agent_sheet_folds_like_everything_else(client, monkeypatch):
    """It is the tallest thing on the page — six rows and a save footer — and a
    settled tenant scrolls past all of it to reach what is below."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert 'data-fold="Your agent" data-group' in html
    head = html[:html.index('data-fold="Your agent"')].rsplit("<details", 1)[1]
    assert " open" not in head,         "closed by default (Kris 2026-09-01) — a settled tenant scrolls past it"


def test_a_group_with_unfinished_setup_inside_cannot_be_left_shut(client, monkeypatch):
    """A collapsed sheet that hides its own amber row would undo the flag.

    Load-bearing since the sheet went closed-by-default (Kris 2026-09-01):
    data-needs only stops the fold MEMORY reclosing it, so the server still has
    to render it open, or unfinished setup is now hidden on first paint."""
    def head(html):
        return html[:html.index('data-fold="Your agent"')].rsplit("<details", 1)[1]

    _env(monkeypatch, cfg={"voice_learning_status": "insufficient"})
    html = client.get("/app/settings").text
    summary = re.search(r'<summary data-fold="Your agent"[^>]*>', html).group()
    assert "data-needs" in summary
    assert " open" in head(html), "unfinished setup must be open on arrival"

    # ...and once everything inside is settled, it is free to close
    _env(monkeypatch, cfg={"voice_profile_source": "auto_learned"})
    html = client.get("/app/settings").text
    summary = re.search(r'<summary data-fold="Your agent"[^>]*>', html).group()
    assert "data-needs" not in summary
    assert " open" not in head(html)


def test_configuration_gathers_the_module_settings_pages(client, monkeypatch):
    """Every module kept its own settings page, reachable only from inside that
    module — while the page called Settings did not mention them."""
    _env(monkeypatch, modules=("crm", "scheduling", "tms", "fsm"))
    monkeypatch.setattr(dashboard.db, "module_ui_visible", lambda *a, **k: True)
    html = client.get("/app/settings").text
    for href in ("/app/diary/intake", "/app/crm/settings", "/app/crm/fields",
                 "/app/invoices/settings"):
        assert f'href="{href}"' in html, href


def test_a_tenant_without_a_module_is_not_offered_its_config(client, monkeypatch):
    _env(monkeypatch, modules=())
    html = client.get("/app/settings").text
    assert '/app/crm/settings' not in html
    assert '/app/tms/pricing' not in html


def test_the_portal_sits_below_the_things_every_tenant_configures(client, monkeypatch):
    """It is on its way to being an add-on, so it stops sitting in the middle of
    the core settings."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert html.index('data-fold="Notifications"') < html.index('data-fold="Customer Portal"')
    assert html.index('data-fold="Customer Portal"') < html.index('data-fold="Close account"')


def test_a_deep_link_opens_the_folds_it_lives_inside(client, monkeypatch):
    """Sending mode is inside a collapsible sheet now — opening only the target
    would land the link on a row inside a shut group."""
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert "for (var p = target.parentElement; p; p = p.parentElement)" in html


# ── No sign-off set is said on the Identity face ────────────────────────────
def test_identity_face_says_when_no_sign_off_is_set(client, monkeypatch):
    """The agent has a name and a tone but nothing to sign off with: the closed face
    says so in the amber 'needs you' treatment (it never blocks anything)."""
    _env(monkeypatch, cfg={**SETTLED, "sign_off": None})
    html = client.get("/app/settings").text
    value, _ = _faces(html)["Identity & voice"]
    assert value == "the office team · warm and professional · no sign-off set"
    assert 'data-fold="Identity &amp; voice" data-needs' in html
    # With a sign-off the face is unchanged and not amber.
    _env(monkeypatch)
    html = client.get("/app/settings").text
    value, _ = _faces(html)["Identity & voice"]
    assert "no sign-off set" not in value
    assert 'data-fold="Identity &amp; voice" data-needs' not in html
    # Nothing set at all keeps its own wording.
    _env(monkeypatch, cfg={})
    value, _ = _faces(client.get("/app/settings").text)["Identity & voice"]
    assert "Nothing set" in value and "no sign-off set" not in value
