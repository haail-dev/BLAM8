"""Certificates on contacts — slice 4 (docs/plan-progress-packages.md). A
contact's DATE field may expire and raise a recall N days before it lapses;
a dependant's expiring field carries its own recall lead too. Pins: the
definition's cleaning (only a date expires; recall 1-365), the card note,
the drafter's lines, the contact sweep (raises inside the lead, quiet when a
course is booked, lapsed reads lapsed, 90-day lead on a three-year
certificate), the dependant field's recall, the editor form.

Run: python -m pytest tests/test_certificates.py -q
"""
from datetime import date

from app import crm_fields, dependants_sweep as sweep, requirement_groups as rg

TODAY = date(2026, 9, 5)
FAW = {"id": "d1", "entity": "contact", "key": "first_aid_certificate_expiry", "label": "First aid certificate expiry",
       "field_type": "date", "durability": "expires", "recall_days": 90}
MOT = {"id": "d2", "entity": "contact", "key": "mot_due", "label": "MOT due", "field_type": "date",
       "durability": "expires", "recall_days": 30}
NOTE = {"id": "d3", "entity": "contact", "key": "notes", "label": "Notes", "field_type": "text", "durability": "once"}


def test_only_a_date_expires_and_recall_is_clamped():
    d = crm_fields.validate_definition("contact", "First aid certificate expiry", "date", [], "",
                                       durability="expires", recall_days="90")
    assert d["durability"] == "expires" and d["recall_days"] == 90
    d = crm_fields.validate_definition("contact", "Notes", "text", [], "", durability="expires", recall_days="90")
    assert d["durability"] == "once"
    assert crm_fields.validate_definition("contact", "When", "date", [], "")["recall_days"] == 30
    assert crm_fields.clean_recall("900") == 365 and crm_fields.clean_recall("x") == 30 and crm_fields.clean_recall(0) == 1
    assert [d["key"] for d in crm_fields.expiring([FAW, MOT, NOTE])] == [FAW["key"], MOT["key"]]


def test_the_card_note_and_the_drafter_lines():
    assert crm_fields.expiry_note(FAW, "2026-12-12", TODAY) == "lapses 12th Dec 26 (in 98 days)"
    assert crm_fields.expiry_note(FAW, "2026-09-01", TODAY) == "lapsed 1st Sep 26"
    assert crm_fields.expiry_note(FAW, "2026-09-05", TODAY) == "lapses today"
    assert crm_fields.expiry_note(FAW, None, TODAY) == "" and crm_fields.expiry_note(NOTE, "x", TODAY) == ""
    rec = {"custom_data": {"first_aid_certificate_expiry": "2026-11-20", "mot_due": "2027-06-01"}}
    lines = crm_fields.expiry_lines([FAW, MOT, NOTE], rec, TODAY)
    assert lines[0].startswith("First aid certificate expiry: lapses 20th Nov 26 (in 76 days) — a renewal is due")
    assert lines[1] == "MOT due: lapses 1st Jun 27 (in 269 days)"


def test_the_contact_sweep_raises_inside_the_lead_and_goes_quiet_once_booked():
    kris = {"id": "c1", "name": "Kris Deane", "email": "kris@x.co",
            "custom_data": {"first_aid_certificate_expiry": "2026-11-20"}}       # 76 days out, 90-day lead
    far = {"id": "c2", "name": "Far", "email": "far@x.co", "custom_data": {"first_aid_certificate_expiry": "2027-06-01"}}
    lapsed = {"id": "c3", "name": "Lapsed", "email": "lapsed@x.co", "custom_data": {"first_aid_certificate_expiry": "2026-08-01"}}
    out = sweep.due_contacts([FAW, NOTE], [kris, far, lapsed], {}, TODAY)
    assert [(o["contact"]["name"], o["lapsed"]) for o in out] == [("Kris Deane", False), ("Lapsed", True)]
    title, body = sweep.phrase_contact(out[0])
    assert title == "Kris Deane's first aid certificate expiry is due 20th Nov 26"
    assert body == "Nothing booked for Kris Deane yet — a good moment to get in touch about their first aid certificate expiry."
    assert sweep.phrase_contact(out[1])[0] == "Lapsed's first aid certificate expiry lapsed 1st Aug 26"
    # a booked course is the stand-down
    booked = {"kris@x.co": [{"id": "b1", "booking_ref": "BK-1", "booking_date": "2026-10-03"}]}
    assert [o["contact"]["name"] for o in sweep.due_contacts([FAW], [kris, far, lapsed], booked, TODAY)] == ["Lapsed"]
    # a past booking is not a stand-down; no expiring definition → nothing
    past = {"kris@x.co": [{"id": "b0", "booking_ref": "BK-0", "booking_date": "2026-01-03"}]}
    assert len(sweep.due_contacts([FAW], [kris], past, TODAY)) == 1
    assert sweep.due_contacts([NOTE], [kris], {}, TODAY) == []
    # the 30-day field stays quiet at 76 days
    kris2 = {**kris, "custom_data": {"mot_due": "2026-11-20"}}
    assert sweep.due_contacts([MOT], [kris2], {}, TODAY) == []


def test_a_dependants_expiring_field_carries_its_own_recall():
    f = rg.clean_field({"label": "Insurance renewal", "type": "date", "durability": "expires", "recall_days": "60"})
    assert f["recall_days"] == 60
    assert rg.clean_field({"label": "Insurance renewal", "durability": "expires"})["recall_days"] == 30
    assert "recall_days" not in rg.clean_field({"label": "Breed", "durability": "once"})
    groups = rg.parse_editor_form(["Vehicle"], ["Vehicles"], ["many"], ["0", "0"], ["MOT due", "Reg"],
                                  ["date", "text"], ["1", "1"], ["expires", "once"], ["0", "0"], ["", ""],
                                  f_recall=["45", ""])
    assert groups[0]["fields"][0]["recall_days"] == 45
    # the sweep honours it: 45 days out of a 45-day lead is a recall; 30 would have been quiet
    inst = [{"id": "i1", "group_id": "g1", "contact_id": "c1", "name": "The van",
             "data": {"mot_due": "2026-10-20"}}]
    g = [{**groups[0], "id": "g1"}]
    out = sweep.due(g, inst, {}, TODAY)
    assert out and out[0]["recall"]
    g30 = rg.clean_groups([{"label": "Vehicle", "fields": [{"label": "MOT due", "durability": "expires"}]}])
    assert sweep.due([{**g30[0], "id": "g1"}], inst, {}, TODAY) == []


def test_the_catalogue_says_how_to_write_a_recall():
    from app import settings_recommend
    assert "expires N" in settings_recommend.PAGES["dependants"]["fields"]["field"]


# ── a validity from the date on record (run #8, 2026-09-05) ──────────────────
THEORY = {"id": "d4", "entity": "contact", "key": "theory_test_pass_date", "label": "Theory test pass date",
          "field_type": "date", "durability": "expires", "recall_days": 60, "valid_months": 24}


def test_a_validity_moves_the_expiry_off_the_date_on_record():
    d = crm_fields.validate_definition("contact", "Theory test pass date", "date", [], "",
                                       durability="expires", recall_days="60", valid_months="24")
    assert d["valid_months"] == 24 and d["recall_days"] == 60
    # a plain date keeps 0; a validity on a non-expiring field is dropped; clamped to ten years
    assert crm_fields.validate_definition("contact", "When", "date", [], "")["valid_months"] == 0
    assert crm_fields.validate_definition("contact", "When", "date", [], "", durability="once", valid_months="30")["valid_months"] == 0
    assert crm_fields.clean_valid("99999") == 600
    assert crm_fields.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28) and crm_fields.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28) and crm_fields.clean_valid("x") == 0 and crm_fields.clean_valid(-5) == 0
    assert crm_fields.expiry_date(THEORY, "2026-03-03") == date(2028, 3, 3)
    assert crm_fields.expiry_date(FAW, "2026-12-12") == date(2026, 12, 12)          # no validity: the date is the expiry
    assert crm_fields.expiry_date(NOTE, "2026-12-12") is None
    # the card note and the drafter's line read the EXPIRY, not the pass date
    assert crm_fields.expiry_note(THEORY, "2026-03-03", TODAY) == "lapses 3rd Mar 28 (in 545 days)"
    assert crm_fields.expiry_note(THEORY, "2024-08-01", TODAY) == "lapsed 1st Aug 26"
    lines = crm_fields.expiry_lines([THEORY], {"custom_data": {"theory_test_pass_date": "2024-10-01"}}, TODAY)
    assert lines == ["Theory test pass date: lapses 1st Oct 26 (in 26 days) — a renewal is due (the business recalls 60 days ahead)"]


def test_the_contact_sweep_recalls_on_the_expiry_not_the_pass_date():
    fresh = {"id": "c1", "name": "Kris", "email": "kris@x.co", "custom_data": {"theory_test_pass_date": "2026-03-03"}}
    soon = {"id": "c2", "name": "Sam", "email": "sam@x.co", "custom_data": {"theory_test_pass_date": "2024-10-01"}}
    out = sweep.due_contacts([THEORY], [fresh, soon], {}, TODAY)
    assert [o["contact"]["name"] for o in out] == ["Sam"]
    assert out[0]["date"] == date(2026, 10, 1) and not out[0]["lapsed"]
    assert sweep.phrase_contact(out[0])[0] == "Sam's theory test pass date is due 1st Oct 26"
