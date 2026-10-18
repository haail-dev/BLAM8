"""The waiting list joins the CRM, and the business's own words replace the
baked-in "class" (Kris 2026-09-05: "Could add the waiting list option to
connect to the CRM somehow? I hope all the changes are agnostic, config not
hardcoded"). Pins: the nouns' cleaning and defaults; the prompt blocks and
refusals in the tenant's words; the two stage settings; a join writes a
timeline row and moves (or creates) the deal only when a stage is named.

Run: python -m pytest tests/test_waitlist_crm_nouns.py -q
"""
from datetime import date

from app import courses, customer_log as clog, diary_config, makeups, wallet


def test_nouns_clean_and_default():
    assert diary_config.clean_nouns({}) == {"booking": "booking", "participant": "customer"}
    assert diary_config.clean_nouns({"booking": " Lesson ", "participant": "PUPIL"}) == {"booking": "lesson", "participant": "pupil"}
    assert diary_config.nouns_of({"nouns": {"booking": "class"}})["booking"] == "class"
    assert diary_config.plural("lesson") == "lessons" and diary_config.plural("class") == "classes"


def test_the_rules_speak_the_tenants_words():
    driving = {"booking": "lesson", "participant": "pupil"}
    txt = courses.render_for_prompt({"waitlist": True, "courses": True}, driving)
    assert "when the lesson, time or DATES" in txt and "a pupil normally books" in txt and "class" not in txt
    mk = makeups.render_for_prompt({"on": True}, driving)
    assert "a lesson missed with at least 24 hours" in mk and "class" not in mk
    c = makeups.clean({"on": True})
    assert makeups.refusal("BK-9", [], [], c, date(2026, 9, 5), driving) == "BK-9 is not one of this customer's lessons."
    both = diary_config.render_all_for_prompt({"nouns": driving, "courses": {"waitlist": True}, "makeups": {"on": True}})
    assert "lesson" in both and "class" not in both
    # the defaults stay neutral
    assert "a booking is full" in courses.render_for_prompt({"courses": True})


def test_wallet_and_log_blocks_take_the_words(monkeypatch):
    monkeypatch.setattr(wallet, "list_credits", lambda *a, **k: [{"package_name": "Block", "unit": "hour", "quantity": 10, "used": 2, "paid": True}])
    assert "after each lesson" in wallet.context_block("t1", "c1", today=date(2026, 9, 5), nouns={"booking": "lesson"})
    monkeypatch.setattr(clog, "list_for_contact", lambda *a, **k: [{"author": "customer", "body": "x", "created_at": "2026-09-05", "booking_ref": "BK-1"}])
    assert "notes on their lessons" in clog.context_block("t1", "kris@x.co", nouns={"booking": "lesson"})
    assert "notes on their bookings" in clog.context_block("t1", "kris@x.co")


def test_stage_settings_clean():
    c = courses.clean({"waitlist": True, "waitlist_stage": "  Waiting list ", "booked_stage": "Enrolled"})
    assert c["waitlist_stage"] == "Waiting list" and c["booked_stage"] == "Enrolled"
    assert courses.clean({})["waitlist_stage"] == "" and courses.clean({})["booked_stage"] == ""


def test_a_join_lands_on_the_timeline_and_moves_the_deal_only_when_named(monkeypatch):
    from app import crm, db
    activities, moves, created = [], [], []
    monkeypatch.setattr(db, "crm_contact_by_email", lambda t, e, client=None: {"id": "c1", "email": e, "name": "Kris"})
    monkeypatch.setattr(crm, "note_contact_activity", lambda t, c, kind, **k: activities.append((kind, k.get("subject"))) or True)
    monkeypatch.setattr(db, "crm_list_stages", lambda t, client=None: [{"id": "s1", "name": "New enquiry"}, {"id": "s2", "name": "Waiting list"}])
    monkeypatch.setattr(db, "crm_list_deals", lambda t, contact_id=None, client=None: [{"id": "d1", "status": "open", "stage_id": "s1"}])
    monkeypatch.setattr(db, "crm_update_deal", lambda d, t, f, client=None: moves.append((d, f["stage_id"])))
    monkeypatch.setattr(db, "crm_create_deal", lambda t, f: created.append(f) or {"id": "dn"})
    courses._crm("t1", "kris@x.co", "joined", "Joined the waiting list for X — number 1", stage_name="")
    assert activities == [("waitlist", "Joined the waiting list for X — number 1")] and moves == []
    courses._crm("t1", "kris@x.co", "joined", "Joined the waiting list for X — number 1", stage_name="waiting LIST")
    assert moves == [("d1", "s2")]
    # no open deal → one is created in the named stage
    monkeypatch.setattr(db, "crm_list_deals", lambda t, contact_id=None, client=None: [])
    courses._crm("t1", "kris@x.co", "booked", "Booked from the waiting list — X (BK-1)", stage_name="Waiting list")
    assert created and created[0]["stage_id"] == "s2" and created[0]["primary_contact_id"] == "c1"
    # an unknown stage name moves nothing
    moves.clear(); created.clear()
    courses._crm("t1", "kris@x.co", "joined", "x", stage_name="Nowhere")
    assert moves == [] and created == []
