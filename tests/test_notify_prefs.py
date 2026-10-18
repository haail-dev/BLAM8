"""Total control over every type of notification.

Kris, 2026-09-02: "We get notifications from all sorts of actions on BLAM8, it
would be good to have total control over every type of notification."

Before this there was none: push.notify() pushed all twenty kinds to every
subscribed device, and the only switch in the app was the morning brief's own
on/off — one kind out of twenty.

The three properties that make it safe to put switches on notifications at all:

  * nothing is ever LOST — the feed row is written whatever the setting, so
    "never" means never interrupt me, not pretend it did not happen;
  * everything held has somewhere to go — a family set to "brief", and anything
    caught by quiet hours, is mentioned in the morning rather than dropped;
  * every failure mode is LOUD — an unreadable setting, an unknown kind, a
    config read that errors: all of them deliver.
"""
import re
from datetime import datetime, timedelta

import pytest

from app import notify_prefs as prefs
from app.scheduling import DIARY_TZ


def at(hour, minute=0):
    return datetime(2026, 9, 2, hour, minute, tzinfo=DIARY_TZ)


ALL_NOW = {c: "now" for c in prefs.FAMILIES}
QUIET = dict(ALL_NOW, quiet_start="22:00", quiet_end="07:00")


# ── The map itself ─────────────────────────────────────────────────────────
def test_every_kind_the_app_can_send_is_either_governed_or_named_ungoverned():
    """The guard against a notification nobody can switch off. If a new
    push.notify(kind=…) lands with no family, this fails and somebody decides
    where it belongs — rather than it quietly being un-silenceable forever."""
    import re
    import glob

    sent = set()
    for path in glob.glob("src/app/**/*.py", recursive=True):
        if "/test" in path:
            continue
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"(?:push\.)?notify\w*\s*\(", src):
            seg = src[m.end():m.end() + 900]
            depth, end = 1, 0
            for i, ch in enumerate(seg):
                depth += (ch == "(") - (ch == ")")
                if depth == 0:
                    end = i
                    break
            k = re.search(r'kind=["\']([a-z_]+)["\']', seg[:end])
            if k:
                sent.add(k.group(1))

    assert sent, "found no notification call sites — the scan broke"
    homeless = {k for k in sent
                if prefs.family_of(k) is None and k not in prefs.UNGOVERNED}
    assert not homeless, (
        "kinds with no family and no reason to be ungoverned: %s" % sorted(homeless))


def test_no_kind_belongs_to_two_families():
    seen = {}
    for col, fam in prefs.FAMILIES.items():
        for kind in fam["kinds"]:
            assert kind not in seen, f"{kind} is in {seen[kind]} and {col}"
            seen[kind] = col


def test_every_family_says_what_it_covers():
    for col, fam in prefs.FAMILIES.items():
        assert fam["label"] and fam["help"] and fam["kinds"], col
        assert col.startswith("notify_"), "the key is the tenant_config column"


# ── Failing loud, never silent ─────────────────────────────────────────────
@pytest.mark.parametrize("cfg", [None, {}, {"notify_drafts": ""},
                                 {"notify_drafts": "nonsense"},
                                 {"notify_drafts": None}])
def test_an_unreadable_setting_still_delivers(cfg):
    """Every way a setting can be missing or junk resolves to 'now'. The failure
    mode of a broken switch has to be a notification you still get."""
    assert prefs.mode(cfg, "draft_ready") == prefs.NOW
    assert prefs.should_push(cfg, "draft_ready", at(10)) is True


def test_a_kind_nobody_has_classified_still_delivers():
    """A new notification shipped before somebody gave it a family shows up,
    gets noticed, and gets one — it does not vanish."""
    assert prefs.mode(ALL_NOW, "some_new_thing_2027") == prefs.NOW
    assert prefs.should_push(ALL_NOW, "some_new_thing_2027", at(3)) is True


@pytest.mark.parametrize("bad", [{"quiet_start": "22:00"},          # half a window
                                 {"quiet_end": "07:00"},
                                 {"quiet_start": "x", "quiet_end": "y"},
                                 {"quiet_start": "22:00", "quiet_end": "22:00"}])
def test_half_or_broken_quiet_hours_silence_nothing(bad):
    cfg = dict(ALL_NOW, **bad)
    assert prefs.in_quiet_hours(cfg, at(3)) is False
    assert prefs.should_push(cfg, "draft_ready", at(3)) is True
    assert prefs.quiet_window(cfg) is None


# ── Quiet hours ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hour, quiet", [(21, False), (22, True), (23, True),
                                         (0, True), (3, True), (6, True),
                                         (7, False), (12, False)])
def test_quiet_hours_wrap_midnight(hour, quiet):
    """22:00 to 07:00 is ONE night, not two windows. Start is inclusive, end
    exclusive — at 07:00 sharp you are back on duty."""
    assert prefs.in_quiet_hours(QUIET, at(hour)) is quiet


def test_a_daytime_window_does_not_wrap():
    cfg = dict(ALL_NOW, quiet_start="09:00", quiet_end="17:00")
    assert prefs.in_quiet_hours(cfg, at(12)) is True
    assert prefs.in_quiet_hours(cfg, at(20)) is False
    assert prefs.in_quiet_hours(cfg, at(3)) is False


def test_nothing_held_overnight_is_dropped():
    """The promise quiet hours make: no buzz, but you hear about it at
    breakfast. Held and never mentioned would be worse than buzzing."""
    assert prefs.should_push(QUIET, "draft_ready", at(3)) is False
    assert prefs.belongs_in_brief(QUIET, "draft_ready", at(3)) is True


# ── The three settings ─────────────────────────────────────────────────────
def test_now_interrupts_and_is_not_repeated_in_the_brief():
    assert prefs.should_push(ALL_NOW, "quote_accepted", at(10)) is True
    assert prefs.belongs_in_brief(ALL_NOW, "quote_accepted", at(10)) is False


def test_brief_never_interrupts_and_always_gets_mentioned():
    cfg = dict(ALL_NOW, notify_quotes="brief")
    assert prefs.should_push(cfg, "quote_accepted", at(10)) is False
    assert prefs.belongs_in_brief(cfg, "quote_accepted", at(10)) is True


def test_never_does_neither():
    cfg = dict(ALL_NOW, notify_invoices="never")
    assert prefs.should_push(cfg, "invoice_paid", at(10)) is False
    assert prefs.belongs_in_brief(cfg, "invoice_paid", at(10)) is False


def test_a_family_only_governs_its_own_kinds():
    cfg = dict(ALL_NOW, notify_quotes="never")
    assert prefs.should_push(cfg, "quote_accepted", at(10)) is False
    assert prefs.should_push(cfg, "invoice_paid", at(10)) is True
    assert prefs.should_push(cfg, "draft_ready", at(10)) is True


# ── The brief cannot silence or contain itself ─────────────────────────────
def test_the_brief_is_ungoverned_and_never_queued_into_itself():
    """It has its own on/off and time, and a brief that could be routed into
    the brief is a loop. Quiet hours do not gag it either — you chose the hour
    it arrives, so it is never a surprise."""
    assert "morning_debrief" in prefs.UNGOVERNED
    assert prefs.should_push(QUIET, "morning_debrief", at(3)) is True
    assert prefs.belongs_in_brief(QUIET, "morning_debrief", at(3)) is False


def test_platform_plumbing_is_not_a_tenants_business():
    for kind in ("support_ticket", "operator_signup"):
        assert kind in prefs.UNGOVERNED
        assert prefs.family_of(kind) is None


# ── The face ───────────────────────────────────────────────────────────────
def test_the_face_reads_back_what_is_actually_reaching_you():
    assert prefs.summary(dict(ALL_NOW, debrief_time="08:15")) == \
        "Everything, as it happens · brief 08:15"
    mixed = dict(ALL_NOW, notify_quotes="brief", notify_invoices="never",
                 quiet_start="22:00", quiet_end="07:00", debrief_time="07:30")
    assert prefs.summary(mixed) == \
        "4 now · 1 in the brief · 1 off · quiet 22:00–07:00 · brief 07:30"


def test_the_face_can_never_say_nothing(monkeypatch):
    """"Nothing" became unreachable when Problems arrived (2026-09-04): switch
    every family off and the one that refuses to be switched off still reads
    `now`, so the face says so rather than claiming silence it cannot deliver."""
    everything_off = {c: "never" for c in prefs.FAMILIES}
    assert prefs.summary(everything_off) == "1 now · 5 off · brief 07:30"


def test_the_face_says_when_there_is_no_brief():
    assert "no brief" in prefs.summary(dict(ALL_NOW, debrief_enabled=False))


# ── "In the brief" with no brief is Never wearing the wrong word ───────────
# Kris 2026-09-04. The three settings only work because the third one has a
# destination; with the brief switched off, a family set to "brief" neither
# buzzes nor gets mentioned, and the page said nothing about it. The option
# stays selectable — the fix is one checkbox further down the same form — but
# the page has to admit what the combination currently does.
def _settings_page(monkeypatch, cfg):
    from fastapi.testclient import TestClient

    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app

    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "google", "mailbox": "hello@acme.co.uk"})
    monkeypatch.setattr(dashboard.db, "get_connection_status",
                        lambda *a, **k: {"sub_expires": None})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.portal, "ensure_portal_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(dashboard.storage, "banner_public_url", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        return TestClient(app).get("/app/settings").text
    finally:
        app.dependency_overrides.clear()


WARNING = "wait for a morning brief you've turned off"


def test_holding_a_family_for_a_brief_that_never_comes_says_so(monkeypatch):
    html = _settings_page(monkeypatch, dict(ALL_NOW, notify_quotes="brief",
                                            notify_invoices="brief",
                                            debrief_enabled=False))
    assert WARNING in html
    assert "2 of these" in html          # counts the families actually stranded


def test_no_warning_when_the_brief_is_there_to_keep_the_promise(monkeypatch):
    html = _settings_page(monkeypatch, dict(ALL_NOW, notify_quotes="brief",
                                            debrief_enabled=True))
    assert WARNING not in html


def test_no_warning_when_nothing_is_waiting_on_the_brief(monkeypatch):
    """The brief being off is a choice, not a fault — only a stranded family is."""
    html = _settings_page(monkeypatch, dict(ALL_NOW, debrief_enabled=False))
    assert WARNING not in html


# ── Delivery actually consults it ──────────────────────────────────────────
def _real_push():
    """A fresh copy of src/app/push.py.

    conftest's autouse `_no_push_sends` replaces push.notify and
    push.notify_draft_ready with no-ops for every test in the suite — so a test
    that calls them through the shared module is asserting against a stub, and
    passes whatever the real code does. (It did: removing the draft-path gate
    left this green until the mutation run caught it.) Loading the file under
    its own name gets the real functions back without disturbing anyone else.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("push_under_test", "src/app/push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_both_push_paths_are_gated(monkeypatch):
    """notify_draft_ready has its own send path. A switch wired to one of two
    paths is worse than no switch, so both are checked here."""
    push = _real_push()

    monkeypatch.setattr(push, "push_enabled", lambda: True)
    monkeypatch.setattr(push, "_record_notification", lambda *a, **k: None)
    monkeypatch.setattr(push, "_trace_send", lambda *a, **k: None)
    monkeypatch.setattr(push.db, "get_tenant_config",
                        lambda *a, **k: dict(ALL_NOW, notify_drafts="never",
                                             notify_quotes="never"))
    sent = []
    monkeypatch.setattr(push.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: sent.append(k)})())

    push.notify("t1", title="x", body="y", url="/z", tag="t", kind="quote_accepted")
    push.notify_draft_ready("t1", contact_name="A", contact_address="a@b.c",
                            subject="s", log_id="1")
    assert sent == [], "a silenced family still pushed"

    # and the same two calls DO push once the families allow it — otherwise
    # this test would pass just as well against a push that never fires
    monkeypatch.setattr(push.db, "get_tenant_config", lambda *a, **k: dict(ALL_NOW))
    push.notify("t1", title="x", body="y", url="/z", tag="t", kind="quote_accepted")
    push.notify_draft_ready("t1", contact_name="A", contact_address="a@b.c",
                            subject="s", log_id="1")
    assert len(sent) == 2, "the gate is refusing everything"


# ── The feed is the record, ALWAYS — including the two opt-ins ─────────────
# Kris 2026-09-04. quote_viewed and invoice_viewed were checked at their call
# sites, which wrapped the whole push.notify in `if cfg.get(...)`. That skipped
# the FEED ROW as well as the push, so a tenant with the toggle off had no
# record their customer had ever opened the document — the last break in the
# contract, and since the deck started reading the feed, an invisible one.
@pytest.mark.parametrize("kind, col", [("quote_viewed", "notify_quote_viewed"),
                                       ("invoice_viewed", "notify_invoice_viewed")])
def test_an_opted_out_view_is_still_written_down(monkeypatch, kind, col):
    push = _real_push()
    monkeypatch.setattr(push, "push_enabled", lambda: True)
    monkeypatch.setattr(push, "_trace_send", lambda *a, **k: None)
    monkeypatch.setattr(push.db, "get_tenant_config",
                        lambda *a, **k: dict(ALL_NOW, **{col: False}))
    monkeypatch.setattr(push.events, "publish", lambda *a, **k: None)
    rows, sent = [], []
    monkeypatch.setattr(push.db, "add_concierge_notification",
                        lambda tid, **kw: rows.append(kw))
    monkeypatch.setattr(push.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: sent.append(k)})())

    push.notify("t1", title="x", body="y", url="/z", tag="t", kind=kind)
    assert [r["kind"] for r in rows] == [kind], "the fact was not recorded"
    assert sent == [], "an opted-out view still buzzed"


@pytest.mark.parametrize("kind, col, on_by_default", [
    ("quote_viewed", "notify_quote_viewed", True),
    ("invoice_viewed", "notify_invoice_viewed", False)])
def test_the_two_opt_ins_keep_their_own_defaults(kind, col, on_by_default):
    """A customer opening a QUOTE is how you know to follow up, so that one is
    on. An invoice being opened rarely needs anything doing, so that one is
    off — and it had no interface at all until 2026-09-02."""
    assert prefs.should_push({}, kind) is on_by_default
    assert prefs.should_push({col: True}, kind) is True
    assert prefs.should_push({col: False}, kind) is False


def test_turning_a_view_off_also_keeps_it_out_of_the_brief():
    """Off means off: not a buzz, and not a mention at breakfast either."""
    assert prefs.belongs_in_brief({"notify_quotes": "brief",
                                   "notify_quote_viewed": False},
                                  "quote_viewed") is False
    # …while the rest of the family still rides into the brief as asked.
    assert prefs.belongs_in_brief({"notify_quotes": "brief",
                                   "notify_quote_viewed": False},
                                  "quote_accepted") is True


def test_an_opt_out_governs_only_its_own_kind():
    cfg = {"notify_quote_viewed": False}
    assert prefs.should_push(cfg, "quote_accepted") is True
    assert prefs.should_push(cfg, "quote_keeps_looking") is True
    assert prefs.opted_out(cfg, "quote_accepted") is False


def test_a_kind_with_no_opt_in_can_never_be_opted_out():
    assert prefs.opted_out({"notify_quote_viewed": False}, "draft_ready") is False
    assert prefs.opted_out(None, "quote_viewed") is False   # None cfg = defaults


def _checked(html: str, name: str) -> bool:
    box = re.search(r'<input type="checkbox" name="%s".*?>' % name, html, re.S)
    assert box, "no %s checkbox on the page" % name
    return "checked" in box.group(0)


def test_the_boxes_read_back_the_defaults_the_code_actually_uses(monkeypatch):
    """The template used to restate both defaults in Jinja — a second copy of
    OPT_INS, and the same kind of drift that let the call-site gate contradict
    the feed contract for months. It reads notify_optin_on now, so this pins
    that the page and the delivery path agree."""
    unset = _settings_page(monkeypatch, dict(ALL_NOW))
    assert _checked(unset, "notify_quote_viewed") is True
    assert _checked(unset, "notify_invoice_viewed") is False

    flipped = _settings_page(monkeypatch, dict(ALL_NOW, notify_quote_viewed=False,
                                               notify_invoice_viewed=True))
    assert _checked(flipped, "notify_quote_viewed") is False
    assert _checked(flipped, "notify_invoice_viewed") is True


def test_a_config_read_that_errors_delivers_anyway(monkeypatch):
    """Failing open is the whole safety story: a database blip must not
    silence a business's notifications."""
    push = _real_push()

    def boom(*a, **k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(push.db, "get_tenant_config", boom)
    assert push._prefs("t1") == {}
    assert prefs.should_push(push._prefs("t1"), "draft_ready", at(3)) is True


# ── Email, the second channel (Kris 2026-09-02) ────────────────────────────
MONITORED = "hello@acme.co.uk"


def test_email_is_off_until_an_address_is_given():
    assert prefs.email_target(ALL_NOW, MONITORED) is None
    assert prefs.email_target(dict(ALL_NOW, notify_email=""), MONITORED) is None
    assert prefs.email_target(dict(ALL_NOW, notify_email="  "), MONITORED) is None


def test_a_real_address_is_normalised():
    cfg = dict(ALL_NOW, notify_email="  Kris@Acme.co.uk ")
    assert prefs.email_target(cfg, MONITORED) == "kris@acme.co.uk"


@pytest.mark.parametrize("junk", ["kris", "kris@", "@acme.co.uk", "kris@acme",
                                  "a b@acme.co.uk"])
def test_something_that_is_not_an_address_turns_the_channel_off(junk):
    assert prefs.email_target(dict(ALL_NOW, notify_email=junk), MONITORED) is None


@pytest.mark.parametrize("address", ["hello@acme.co.uk", "HELLO@ACME.CO.UK",
                                     "  hello@acme.co.uk  "])
def test_it_refuses_the_mailbox_the_agent_is_reading(address):
    """The mail is sent FROM the tenant's connected mailbox, because there is no
    transactional sender in the product. Addressed back at that same inbox, a
    notification lands in the pile the agent drafts against — so this is the one
    address the channel will not use, however it is typed."""
    assert prefs.email_target(dict(ALL_NOW, notify_email=address), MONITORED) is None


def test_email_obeys_the_same_settings_as_push(monkeypatch):
    """It is a channel, not an escape hatch: "in the brief" and quiet hours
    silence mail exactly as they silence a buzz."""
    push = _real_push()
    monkeypatch.setattr(push, "push_enabled", lambda: False)      # isolate email
    monkeypatch.setattr(push, "_record_notification", lambda *a, **k: None)
    monkeypatch.setattr(push, "_trace_send", lambda *a, **k: None)
    monkeypatch.setattr(push.db, "get_tenant", lambda *a, **k: {"mailbox": MONITORED})

    started = []
    monkeypatch.setattr(push.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: started.append(k.get("name"))})())

    def cfg(**over):
        return dict(ALL_NOW, notify_email="kris@acme.co.uk", **over)

    monkeypatch.setattr(push.db, "get_tenant_config", lambda *a, **k: cfg())
    push.notify("t1", title="x", body="y", url="/z", tag="a", kind="quote_accepted")
    assert len(started) == 1, "an allowed notification sent no mail"

    started.clear()
    monkeypatch.setattr(push.db, "get_tenant_config",
                        lambda *a, **k: cfg(notify_quotes="brief"))
    push.notify("t1", title="x", body="y", url="/z", tag="b", kind="quote_accepted")
    assert started == [], "a family held for the brief still emailed"

    started.clear()
    monkeypatch.setattr(push.db, "get_tenant_config",
                        lambda *a, **k: cfg(notify_email=MONITORED))
    push.notify("t1", title="x", body="y", url="/z", tag="c", kind="quote_accepted")
    assert started == [], "it emailed the mailbox the agent reads"


def test_a_tenant_lookup_that_fails_sends_no_mail(monkeypatch):
    """Unlike the settings read, this one fails CLOSED: without the monitored
    address we cannot prove the mail isn't aimed at the agent's own inbox, and
    a notification loop is worse than a missed notification."""
    push = _real_push()
    monkeypatch.setattr(push, "push_enabled", lambda: False)
    monkeypatch.setattr(push, "_record_notification", lambda *a, **k: None)
    monkeypatch.setattr(push, "_trace_send", lambda *a, **k: None)
    monkeypatch.setattr(push.db, "get_tenant_config",
                        lambda *a, **k: dict(ALL_NOW, notify_email="kris@acme.co.uk"))

    def boom(*a, **k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(push.db, "get_tenant", boom)
    started = []
    monkeypatch.setattr(push.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: started.append(1)})())
    push.notify("t1", title="x", body="y", url="/z", tag="t", kind="quote_accepted")
    assert started == []
