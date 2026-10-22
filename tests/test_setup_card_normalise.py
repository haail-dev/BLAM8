"""Known model shapes for a first proposal reach the card contract through normalise."""
from copy import deepcopy
import pytest
from app import setup_card_model as model, setup_cards as cards, setup_rows
from tests.test_setup_cards import append, start


def assignment(field, value):
    return {"field": field, "value": value, "source": "owner_said", "reference": "Owner's latest answer"}


def edit(op="append", id="", name="", fields=None, held=None):
    """One change as the help model writes it, before unpack."""
    return {"op": op, "id": id, "name": name, "held": held,
            "fields": [assignment(field, value) for field, value in (fields or {}).items()]}


def proposal(name, *edits, revision=0):
    return {"page": name, "revision": revision, "changes": list(edits)}


def run(raw, current=None):
    """The route's order: unpack, normalise, then the card contract, page by page."""
    entries, notes = model.normalise(model.unpack(raw), current or {})
    built = {e["page"]: cards.change(e["page"], (current or {}).get(e["page"]), e["revision"], "turn-1", e["changes"])
             for e in entries}
    return built, notes


DIARY_VALUES = {"working_days": ["mon", "tue", "wed", "thu", "fri"], "open_time": "09:00", "close_time": "16:00",
                "timezone": "Europe/London", "bank_holiday_nation": "england-and-wales"}


def diary_ids(prefix="diary_settings/"):
    return [proposal("diary_settings", *[edit("set", prefix + key, "", {"value": value}) for key, value in DIARY_VALUES.items()])]


def booking_labels():
    shared = {"required": False, "remember": "ask", "services": []}
    return [proposal("booking_details",
                 edit("append", "", "Site address", {"label": "Site address", "required": True, "kind": "answer", "services": [], "remember": "once"}),
                 edit("append", "", "Fault description", {"label": "Fault description", "kind": "answer"} | shared),
                 edit("append", "", "Preferred day", {"label": "Preferred day", "kind": "answer"} | shared),
                 edit("append", "", "Photos", {"label": "Photos", "kind": "document", "required": False, "remember": "ask", "services": []}),
                 edit("append", "", "Access notes", {"label": "Access notes", "kind": "answer"} | shared))]


def dependants_labels():
    detail = {"type": "text", "required": True, "durability": "once", "gate": False}
    return [proposal("dependants",
                 edit("append", "", "Property", {"label": "Property", "plural": "Properties", "cardinality": "many"}),
                 edit("append", "", "Address", {"kind": "Property", "label": "Address"} | detail),
                 edit("append", "", "Resident name", {"kind": "Property", "label": "Resident name"} | detail),
                 edit("append", "", "Access notes", {"kind": "Property", "label": "Access notes"} | detail | {"required": False}))]


def crm_quiet_days():
    stages = [("Enquiry", "active"), ("Quote sent", "active"), ("Booked", "active"),
              ("Completed", "won"), ("Declined", "lost"), ("Dormant", "dormant")]
    changes = [edit("append", "", "stage", {"name": name, "stage_type": kind, "display_order": order})
               for order, (name, kind) in enumerate(stages, 1)]
    return [proposal("crm_settings", edit("set", "crm_settings/quiet_days", "setting", {"key": "quiet_days", "value": 21}, held=False), *changes)]


SHAPES = {"diary": diary_ids, "diary-bare": lambda: diary_ids(""), "booking": booking_labels,
          "dependants": dependants_labels, "crm": crm_quiet_days}


def fields_of(card):
    return [(line["name"], line["fields"]) for line in card["lines"]]


def test_set_on_catalogue_id_becomes_a_setting_append_on_an_empty_card():
    built, notes = run(diary_ids())
    card = built["diary_settings"]
    assert [(line["name"], line["status"], line["problems"]) for line in card["lines"]] == [("setting", "proposed", [])] * 5
    assert fields_of(card) == [("setting", {"key": key, "value": value}) for key, value in DIARY_VALUES.items()]
    for line in card["lines"]:
        assert line["sources"]["key"] == line["sources"]["value"] == {"kind": "owner_said", "reference": "Owner's latest answer"}
    assert notes == {"diary_settings": "Diary settings: 5 values proposed as new settings."}


def test_bare_key_ids_are_recognised():
    built, notes = run(diary_ids(""))
    assert fields_of(built["diary_settings"]) == [("setting", {"key": key, "value": value}) for key, value in DIARY_VALUES.items()]
    assert notes["diary_settings"] == "Diary settings: 5 values proposed as new settings."


def test_set_on_a_catalogue_id_edits_the_existing_pending_line():
    current = {"diary_settings": start(append("setting", key="timezone", value="Europe/London"))}
    line = current["diary_settings"]["lines"][0]
    for fields in ({"key": "timezone", "value": "Europe/Dublin"}, {"value": "Europe/Dublin"}):
        built, notes = run([proposal("diary_settings", edit("set", "diary_settings/timezone", "", fields), revision=1)], current)
        edited = built["diary_settings"]["lines"]
        assert [v["id"] for v in edited] == [line["id"]]
        assert edited[0]["fields"] == {"key": "timezone", "value": "Europe/Dublin"}
        assert edited[0]["sources"]["key"] == line["sources"]["key"]
        assert edited[0]["sources"]["value"]["reference"] == "Owner's latest answer"
        assert notes == {"diary_settings": "Diary settings: 1 value added to a setting already proposed."}


def test_a_finished_line_does_not_take_the_edit():
    applied = {**start(append("setting", key="timezone", value="Europe/London"))}
    applied["lines"][0]["status"] = "applied"
    built, _ = run([proposal("diary_settings", edit("set", "diary_settings/timezone", "", {"value": "Europe/Dublin"}), revision=1)],
                   {"diary_settings": applied})
    assert [line["status"] for line in built["diary_settings"]["lines"]] == ["proposed"]
    assert built["diary_settings"]["lines"][0]["fields"] == {"key": "timezone", "value": "Europe/Dublin"}


def test_held_is_kept_when_a_set_becomes_an_append():
    entries, _ = model.normalise(model.unpack([proposal("packages", edit("set", "packages/package_low_at", "", {"value": 2}, held=True))]), {})
    assert entries[0]["changes"][0]["op"] == "append" and entries[0]["changes"][0]["held"] is True


def test_label_named_appends_get_their_row_type():
    built, notes = run(booking_labels())
    card = built["booking_details"]
    assert [(line["name"], line["status"], line["problems"]) for line in card["lines"]] == [("field", "proposed", [])] * 5
    assert [line["fields"]["label"] for line in card["lines"]] == ["Site address", "Fault description", "Preferred day", "Photos", "Access notes"]
    assert notes == {"booking_details": "Booking details: 5 rows sorted into the right kind of row."}
    built, _ = run(dependants_labels())
    card = built["dependants"]
    assert [(line["name"], line["status"], line["problems"]) for line in card["lines"]] == [
        ("group", "proposed", []), ("field", "proposed", []), ("field", "proposed", []), ("field", "proposed", [])]
    for line in card["lines"]:
        assert line["fields"]["label"] in setup_rows.describe("dependants", line["name"], line["fields"])
        assert line["fields"]["label"] in line["display"]


@pytest.mark.parametrize("page_name,fields,row_type", [
    ("diary_settings", {"name": "Review", "minutes": 45, "timing": "time"}, "service"),
    ("diary_settings", {"name": "Van", "count": 2}, "resource"),
    ("diary_settings", {"key": "buffer_minutes", "value": 10}, "setting"),
    ("packages", {"name": "Ten sessions", "quantity": 10, "unit": "session"}, "package"),
    ("crm_fields", {"label": "Reference", "field_type": "text"}, "contact_field"),
    ("knowledge", {"label": "Opening hours", "content": "We open at nine."}, "note"),
])
def test_each_page_infers_its_row_type_from_field_names(page_name, fields, row_type):
    entries, _ = model.normalise(model.unpack([proposal(page_name, edit("append", "", "A label", fields))]), {})
    assert entries[0]["changes"][0]["name"] == row_type


def test_crm_set_on_quiet_days_with_six_stage_appends():
    raw = crm_quiet_days()
    assert raw[0]["changes"][0]["name"] == "setting" and raw[0]["changes"][0]["op"] == "set"
    built, notes = run(raw)
    card = built["crm_settings"]
    assert [line["name"] for line in card["lines"]] == ["setting"] + ["stage"] * 6
    assert all(line["status"] == "proposed" and line["problems"] == [] for line in card["lines"])
    assert card["lines"][0]["fields"] == {"key": "quiet_days", "value": 21}
    assert notes == {"crm_settings": "CRM settings: 1 value proposed as a new setting."}


def test_set_may_restate_a_setting_row_type_but_nothing_else():
    patch = model.unpack([proposal("crm_settings", edit("set", "line-1", "setting", {"value": 21}))])[0]["changes"][0]
    assert patch == {"op": "set", "id": "line-1", "fields": {"value": 21}, "sources": {"value": {"kind": "owner_said", "reference": "Owner's latest answer"}}}
    with pytest.raises(cards.Invalid, match="ID only"):
        model.unpack([proposal("crm_settings", edit("set", "line-1", "stage", {"name": "Won"}))])
    with pytest.raises(cards.Invalid, match="ID only"):
        model.unpack([proposal("crm_settings", edit("remove", "line-1", "setting"))])
    with pytest.raises(cards.Invalid, match="ID only"):
        model.unpack([proposal("crm_settings", edit("set", "line-1", "Setting", {"value": 21}))])


def test_unknown_shapes_are_left_for_the_validator():
    unknown = [
        edit("set", "diary_settings/not_a_setting", "", {"value": 1}),
        edit("set", "crm_settings/quiet_days", "", {"value": 21}),           # a catalogue key, but on the wrong page
        edit("set", "timezone", "", {"key": "open_time", "value": "09:00"}),  # fields name a different setting
        edit("set", "timezone", "", {"key": "timezone"}),                     # no value to propose
        edit("append", "", "Odd row", {"colour": "blue"}),
        edit("append", "", "Odd row", {}),
    ]
    for item in unknown:
        raw = [proposal("diary_settings", item)]
        entries, notes = model.normalise(model.unpack(raw), {})
        assert entries == model.unpack(raw) and notes == {}
        with pytest.raises(cards.Invalid):
            cards.change("diary_settings", None, 0, "turn-1", entries[0]["changes"])


@pytest.mark.parametrize("page_name,fields", [
    ("crm_settings", {"stage_type": "active", "display_order": 1}),   # no name to take the stage name from
    ("booking_details", {"required": True}),                            # no label
    ("booking_details", {"label": "Reference", "key": "reference"}),   # a key marks a setting, not a field
    ("dependants", {"kind": "Property", "type": "text"}),
])
def test_a_missing_label_is_never_made_up_from_the_row_name(page_name, fields):
    raw = [proposal(page_name, edit("append", "", "Property address", fields))]
    entries, notes = model.normalise(model.unpack(raw), {})
    assert entries == model.unpack(raw) and notes == {}


def test_normalise_never_mutates_its_input():
    for build in SHAPES.values():
        entries = model.unpack(build())
        before = deepcopy(entries)
        cards_before = {"diary_settings": start(append("setting", key="timezone", value="Europe/London"))}
        snapshot = deepcopy(cards_before)
        rewritten, _ = model.normalise(entries, cards_before)
        assert entries == before and cards_before == snapshot
        assert rewritten is not entries


def test_remove_is_never_rewritten():
    current = {"diary_settings": start(append("setting", key="timezone", value="Europe/London"))}
    raw = [proposal("diary_settings", edit("remove", "diary_settings/timezone"), revision=1)]
    entries, notes = model.normalise(model.unpack(raw), current)
    assert entries == model.unpack(raw) and notes == {}
    with pytest.raises(cards.Invalid, match="pending line"):
        cards.change("diary_settings", current["diary_settings"], 1, "turn-2", entries[0]["changes"])


def test_a_set_on_a_real_line_id_is_never_rewritten():
    current = {"diary_settings": start(append("setting", key="timezone", value="Europe/London"))}
    line_id = current["diary_settings"]["lines"][0]["id"]
    raw = [proposal("diary_settings", edit("set", line_id, "", {"value": "Europe/Dublin"}), revision=1)]
    entries, notes = model.normalise(model.unpack(raw), current)
    assert entries == model.unpack(raw) and notes == {}


def test_considered_notes_and_untouched_pages_pass_through():
    raw = [{"page": "diary_settings", "revision": 0, "changes": [],
            "considered": [{"key": "diary_settings/buffer_minutes", "state": "needs_answer"}]}]
    entries, notes = model.normalise(model.unpack(raw), {})
    assert entries == model.unpack(raw) and notes == {}


def test_refused_change_names_the_first_bad_change_only():
    entries, _ = model.normalise(model.unpack([proposal(
        "diary_settings",
        edit("set", "diary_settings/timezone", "", {"value": "Europe/London"}),
        edit("set", "mystery", "", {"value": 1}),
        edit("set", "mystery-two", "", {"value": 2}))]), {})
    changes = entries[0]["changes"]
    assert model.refused_change("diary_settings", None, 0, changes) is changes[1]
    assert model.refused_change("diary_settings", None, 1, changes) is None   # a stale revision is not a bad change
    assert model.refused_change("diary_settings", None, 0, changes[:1]) is None
