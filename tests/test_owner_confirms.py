"""Who is allowed to change a document-backed fact — and the two bugs beside it.

Kris, 2026-09-21: "Whether or not to allow the AI to overwrite sensitive info
like Document provided infos. If not allowed it should flag a human… If it has
been set to allow, then it continues with its task."

`proof` (shipped earlier the same day) asks "is there a document?". This asks
the question underneath it, and it is the business's call: a kennel with a
licence at stake wants a person to confirm a vaccination expiry however sure the
model is; a dog walker will take the customer's word.

Also pinned here, because both bit during the same take:
  * an attached card filed against the OWNER, not the dog, so neither the proof
    gate nor the confirm chip could see it;
  * four identical gate refusals looping into an empty draft that told the
    owner to "fix it in Diary settings" — when nothing was wrong with the
    settings and the customer simply had not sent a valid date.

Run: python -m pytest tests/test_owner_confirms.py -q
"""
from app import dependants_extract as dx, documents, requirement_groups as rg

GROUP = rg._ensure({"id": "g1", "label": "Dog", "fields": [
    {"label": "Vaccinations", "key": "vaccinations", "type": "date",
     "durability": "expires", "required": True, "gate": True, "set_by": "owner"},
    {"label": "Feeding instructions", "key": "feeding_instructions", "type": "text",
     "durability": "confirm"},
]})
BELLA = {"id": "i1", "group_id": "g1", "name": "Bella", "data": {}}


def _plan(fields, instances=(BELLA,)):
    return dx.plan([GROUP], list(instances), [{
        "group": "Dog", "name": "Bella", "confidence": 0.95,
        "fields": [{"key": k, "value": v, "confidence": 0.95} for k, v in fields.items()],
    }])


# ── the setting ──────────────────────────────────────────────────────────────
def test_a_protected_field_is_never_written_however_sure_the_model_is():
    """0.95 is well past the autofill floor. It still becomes a chip."""
    steps = _plan({"vaccinations": "2027-09-21"})
    assert [s["action"] for s in steps] == ["suggest"]
    assert steps[0]["data"] == {"vaccinations": "2027-09-21"}


def test_an_ordinary_field_on_the_same_dog_still_writes():
    """The setting is per field, not per dependant — it must not freeze the
    whole record."""
    steps = _plan({"feeding_instructions": "Two scoops, morning and evening"})
    assert [s["action"] for s in steps] == ["write"]


def test_the_two_split_when_one_email_carries_both():
    steps = _plan({"vaccinations": "2027-09-21",
                   "feeding_instructions": "Two scoops"})
    by = {s["action"]: s["data"] for s in steps}
    assert by["write"] == {"feeding_instructions": "Two scoops"}
    assert by["suggest"] == {"vaccinations": "2027-09-21"}


def test_customer_is_the_default_so_nothing_changes_for_anyone_else():
    assert rg.clean_field({"label": "Vaccinations", "durability": "expires",
                           "gate": True})["set_by"] == "customer"
    assert rg.clean_field({"label": "Vaccinations", "durability": "expires",
                           "gate": True, "set_by": "owner"})["set_by"] == "owner"


def test_the_editor_form_carries_it():
    g = rg.parse_editor_form(["Dog"], ["Dogs"], ["many"], ["0"], ["Vaccinations"],
                             ["date"], ["1"], ["expires"], ["1"], [""], f_setby=["1"])
    assert g[0]["fields"][0]["set_by"] == "owner"


def test_the_agent_is_told_not_to_promise_on_an_unconfirmed_value():
    block = rg.render_for_prompt([GROUP])
    assert "cannot set this one" in block
    assert "never tell them it is updated or booked" in block


# ── the card belongs to the dog ──────────────────────────────────────────────
def test_one_dependant_on_the_record_takes_the_file():
    assert documents.instance_for([BELLA], "scan001.pdf") == "i1"


def test_the_filename_names_the_dog_when_there_are_several():
    two = [BELLA, {"id": "i2", "name": "Rufus"}]
    assert documents.instance_for(two, "bella-vaccination-card.pdf") == "i1"
    assert documents.instance_for(two, "rufus-card.pdf") == "i2"


def test_the_words_around_the_file_can_name_it_instead():
    two = [BELLA, {"id": "i2", "name": "Rufus"}]
    assert documents.instance_for(two, "scan001.pdf", "Here is Rufus's new card") == "i2"


def test_an_ambiguous_file_is_left_unlinked_rather_than_guessed():
    """A certificate filed against the wrong animal is worse than one filed
    against nobody."""
    two = [BELLA, {"id": "i2", "name": "Rufus"}]
    assert documents.instance_for(two, "scan001.pdf") is None
    assert documents.instance_for(two, "cards.pdf", "Bella and Rufus are both due") is None


def test_an_archived_dependant_is_not_a_candidate():
    assert documents.instance_for([{**BELLA, "archived_at": "2026-01-01"}], "x.pdf") is None


def test_a_name_inside_another_word_is_not_a_match():
    one = [{"id": "i9", "name": "Bo"}]
    assert documents.instance_for(one + [{"id": "i8", "name": "Rufus"}],
                                  "bootcamp-notes.pdf") is None


# ── the empty draft must not blame the settings ──────────────────────────────
GATE_REFUSAL = ("hold_booking", {}, {"error": "missing_info",
                                     "missing": ["Bella: Vaccinations (expired 2026-10-03)"]})


def test_a_gate_refusal_is_reported_as_waiting_on_the_customer():
    from app.pipeline import _gate_gap
    assert _gate_gap({"calls": [GATE_REFUSAL]}) == "Bella: Vaccinations (expired 2026-10-03)"


def test_a_real_configuration_wall_is_not_mistaken_for_one():
    from app.pipeline import _gate_gap
    walls = {"calls": [("hold_booking", {}, {"error": "unsupported",
                                             "message": "no same-day departures"})]}
    assert _gate_gap(walls) == ""
    assert _gate_gap({"calls": []}) == ""
    assert _gate_gap(None) == ""
