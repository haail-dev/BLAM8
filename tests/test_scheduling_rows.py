"""Scheduling reads as a list of decisions, not a wall of inputs.

Kris, 2026-09-01: "Suggested update for Scheduling block?"

It was 48 fields in 19 groups, 37 of them behind a single disclosure called
"Advanced" — which was not a category, but the pile of things nobody grouped —
under a closed face that stated two of the nineteen.

Every group is a row stating its own value now. The restructure was allowed to
change wrappers and nothing else: the form still posts exactly the same fields,
which is what most of this file is about.
"""
import io
import re

import pytest

from tests.test_settings_fold import client, _env      # noqa: F401  (fixture)


PARTIAL = "src/app/templates/app/_scheduling_settings.html"
FORM = re.compile(r'<form[^>]*action="/app/settings/scheduling".*?</form>', re.S)

# Every control the block posts. Written out so that losing one — by mis-slicing
# a <div> while re-wrapping, say — fails loudly instead of silently dropping a
# setting a tenant had already configured.
EXPECTED = {
    "timezone", "timezone_state", "booking_revision", "capacity_state",
    "working_days_state", "opening_hours_state", "confirm_booking_setting",
    "service_ooh_basis", "service_ooh_terms",
    "offer_last_call", "offer_last_call_present",
    "csrf_token", "return_to",
    # base
    "open_time", "close_time", "slot_minutes", "working_days", "blackout_dates",
    # services
    "service_name", "service_minutes", "service_price", "service_resource",
    "service_buffer", "service_recall_months", "service_timing",
    # a service's own booking rules (docs/plan-diary-multiview.md, 2026-09-23)
    "service_from", "service_until", "service_days", "service_max", "service_starts",
    "service_same_day", "service_notice",   # urgent services (D1 stage 1)
    "service_ooh_until", "service_ooh_days", "service_ooh_bh", "service_ooh_price",
    "service_ooh_after", "service_ooh_offer", "bank_holiday_nation", "bank_holidays_open",  # D1 stage 2/3
    "service_after_job", "job_done_when",   # after the job (docs/plan-after-job-tasks.md)
    # the When it's full card (docs/plan-when-its-full.md item 4)
    "full_card_present", "other_dates", "hold_while_gathering",
    # resource types
    "resource_name", "resource_count", "resource_rate", "resource_rate_unit",
    "resource_skills",
    # notice, holds, capacity
    "min_notice_hours", "horizon_days", "offer_ttl_hours", "offer_nudge_enabled",
    "capacity_per_window", "windows",
    # travel  (travel_mode appears only once the travel add-on is active)
    "buffer_minutes",
    # stays
    "stay_rate", "stay_rate_unit", "stay_deposit", "stay_min_nights",
    "checkin_after", "checkout_before",
    # per-day hour overrides
    } | {"dayhours_%s_%s" % (d, e)
         for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
         for e in ("open", "close")} | {
    "party_enabled", "party_max_people",
    "recurring_enabled", "recurring_freq", "recurring_max",
    "delivery_offered", "delivery_fee", "delivery_radius",
    # Cancellations & credits (src/app/wallet.py, slice 2)
    "credits_spend", "late_cancel_hours", "no_show_spends", "excused_refunds", "late_move_charged", "hours_unit",
    # Courses + the waiting list (src/app/courses.py, slice 3)
    "courses_on", "waitlist_on", "waitlist_notify", "offer_hours", "reenrol_days", "reenrol_notify",
    # Make-ups (src/app/makeups.py, slice 6)
    "makeups_on", "makeup_per_term", "makeup_notice_hours", "makeup_within_days", "makeup_no_show_ok", "makeup_free",
    # The business's words + the waiting list's CRM link (2026-09-05)
    "noun_booking", "noun_participant", "waitlist_stage", "booked_stage",
}


def _form(client, path):
    m = FORM.search(client.get(path).text)
    assert m, f"{path} renders no scheduling form"
    return m.group(0)


def _names(form):
    return set(re.findall(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', form))


@pytest.mark.parametrize("path", ["/app/settings", "/app/diary/settings"])
def test_the_block_still_posts_every_field_it_used_to(client, monkeypatch, path):
    """The one thing the restyle must not do. <details> hides but never
    disables, so a folded row's inputs still submit — that is what makes the
    rows safe, and it is the property worth pinning."""
    _env(monkeypatch, modules=("scheduling",))
    names = _names(_form(client, path))
    assert names == EXPECTED, (
        "lost: %s | new: %s" % (sorted(EXPECTED - names), sorted(names - EXPECTED)))


@pytest.mark.parametrize("path", ["/app/settings", "/app/diary/settings"])
def test_no_field_is_disabled_by_being_folded(client, monkeypatch, path):
    _env(monkeypatch, modules=("scheduling",))
    form = _form(client, path)
    assert " disabled" not in form, "a folded field would stop posting"


# ── The shape ──────────────────────────────────────────────────────────────
def test_advanced_is_gone():
    """It held 37 of the 48 fields across 14 unrelated groups. Every one of them
    is a named row now."""
    s = io.open(PARTIAL, encoding="utf-8").read()
    assert "notice, capacity &amp; per-industry options" not in s
    assert not re.search(r">\s*Advanced\s*<", s), "the grab-bag is back"


@pytest.mark.parametrize("path", ["/app/settings", "/app/diary/settings"])
def test_every_group_is_a_row_that_states_its_value(client, monkeypatch, path):
    _env(monkeypatch, modules=("scheduling",))
    form = _form(client, path)
    faces = re.findall(
        r'<span class="text-sm font-semibold text-navy flex-none">([^<]+)</span>\s*'
        r'<span class="flex-1[^"]*">([^<]*)</span>', form)
    named = {n.strip(): v.strip() for n, v in faces}
    for row in ("Opening hours", "Working days", "Blackout dates",
                "Services &amp; durations", "Resource types",
                "Notice, holds &amp; capacity", "Travel", "Stays",
                "Hours by day", "Party size", "Recurring bookings",
                "Delivery &amp; collection"):
        assert row in named, f"{row} is not a row on {path}"
        assert named[row], f"{row} states no value"


def test_a_rows_value_hides_only_when_that_row_opens():
    """The bug this caught: `group-open:` compiles to `.group[open] &`, which
    matches ANY open .group ancestor. Unnamed, every row's value went invisible
    the moment the SHEET was opened — which is the only time you can see them.
    The variant has to be scoped to the row's own named group."""
    s = io.open(PARTIAL, encoding="utf-8").read()
    # the macro every row's face comes from
    macro = s[s.index("{% macro sface"):s.index("{% endmacro %}")]
    assert "group-open/row:invisible" in macro
    assert "group-open:invisible" not in macro, "unscoped — values vanish with the sheet"
    rows = re.findall(r'<details class="(group[^"]*)"', s)
    assert rows and all("group/row" in c for c in rows), rows
    # Exactly one unscoped variant survives, and it is the CARD's own face. That
    # one is correct: the card has no .group ancestor to be confused by, and its
    # value SHOULD go quiet when the card opens.
    assert s.count("group-open:invisible") == 1
    at = s.index("group-open:invisible")
    assert "schedface" in s[at:at + 120], "the unscoped one is not the card's face"


def test_the_face_summarises_the_block_not_two_fields_of_nineteen(client, monkeypatch):
    """It read "mon, tue, wed, thu, fri · 09:00–17:00" — lowercase, and silent
    about the seventeen other things inside."""
    _env(monkeypatch, modules=("scheduling",))
    html = client.get("/app/settings").text
    face = re.search(r'data-fold="Scheduling".*?</summary>', html, re.S).group(0)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", face)).strip()
    assert "mon, tue" not in text, "days are still a lowercase list"
    assert "Mon–Fri" in text, text
    assert "09:00–17:00" in text, text
    assert "slots" in text or "grid" in text or "services" in text, "the face says nothing about length"


def test_help_is_an_offer_not_the_headline():
    """It was a bordered button as big as the section title; on a phone it took
    half the header and broke the sentence above it into four ragged lines.
    Kris 2026-09-25: the button goes altogether (it re-offered the whole card
    after setup). The guided setup still reads its opening question from a
    hidden [data-concierge-ask]; the page shows one quiet "/help" line."""
    s = io.open(PARTIAL, encoding="utf-8").read()
    assert not re.search(r"<button[^>]*data-concierge-ask", s), "the help button is back"
    assert re.search(r"<span hidden data-concierge-ask=\"Set up my diary settings", s),         "the walk-through's question is gone"
    assert 'include "app/_help_hint.html"' in s


def test_a_card_fill_brings_the_pages_own_save_into_view():
    """Run log F5 (plumber take 2026-09-25): "Use recommended settings" filled
    the page, but its Save sat behind the assistant and the next step only
    appeared once Kris found it. After a card fill the panel drops to its
    bubble and the form's Save is scrolled to and flashed; a held prefill
    (typed on arrival, not a card) keeps the old scroll-to-form."""
    js = open("src/app/static/concierge.js", encoding="utf-8").read()
    tail = js.split("pfBanner(form, !!data.held);")[1].split("applySettingsPrefill();")[0]
    assert "if (data.held) {" in tail and "setOpen(false)" in tail
    assert "pfSaveButton(form)" in tail and 'save.scrollIntoView({ behavior: "smooth", block: "center" })' in tail
    assert 'form.querySelector("[data-save-row] button")' in js
