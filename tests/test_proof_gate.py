"""A gated date the customer types is not evidence.

Kris, 2026-09-21, watching the vaccination take back: "surely they should
present the doc to confirm?" He was right, and the rig proved it — Hannah wrote
"the new card says she's covered until 21st September 2027", the extractor read
the sentence, wrote 2027-09-21 to Bella's record, and the gate opened. Bella's
record held zero documents.

Little Paws' own website says "evidence of yearly booster jabs must be provided
annually" — but that sentence lived in the knowledge base, which a model may or
may not act on, while the gate could only ever ask for a value. `proof` puts the
rule where it is enforced: the date says WHEN, the document says it is TRUE.

Run: python -m pytest tests/test_proof_gate.py -q
"""
from app import requirement_groups as rg

GROUP = {"label": "Dog", "fields": [
    {"label": "Vaccinations", "key": "vaccinations", "type": "date",
     "durability": "expires", "required": True, "gate": True, "proof": True},
]}
NO_PROOF_GROUP = {"label": "Dog", "fields": [
    {"label": "Vaccinations", "key": "vaccinations", "type": "date",
     "durability": "expires", "required": True, "gate": True},
]}
IN_DATE = {"vaccinations": "2027-09-21"}
BOOKING = "2026-10-12"


# ── the defect ───────────────────────────────────────────────────────────────
def test_a_date_with_no_certificate_does_not_open_a_proof_gate():
    gaps = rg.missing_for(rg._ensure(GROUP), IN_DATE, BOOKING, has_proof=False)
    assert len(gaps) == 1
    assert "Vaccinations" in gaps[0] and "certificate" in gaps[0]


def test_the_same_date_with_the_certificate_on_file_opens_it():
    assert rg.missing_for(rg._ensure(GROUP), IN_DATE, BOOKING, has_proof=True) == []


def test_without_proof_set_the_gate_behaves_exactly_as_before():
    """Every other tenant's gate is untouched — proof is opt-in per field."""
    assert rg.missing_for(rg._ensure(NO_PROOF_GROUP), IN_DATE, BOOKING, has_proof=False) == []


# ── proof never replaces the date check ──────────────────────────────────────
def test_a_certificate_does_not_excuse_an_expired_date():
    """A card on file for a booster that has run out is still a refusal, and
    the refusal names the expiry — not the missing paperwork."""
    gaps = rg.missing_for(rg._ensure(GROUP), {"vaccinations": "2026-10-03"},
                          BOOKING, has_proof=True)
    assert len(gaps) == 1 and "expired 2026-10-03" in gaps[0]


def test_an_empty_required_field_still_reads_as_simply_missing():
    gaps = rg.missing_for(rg._ensure(GROUP), {}, BOOKING, has_proof=False)
    assert gaps == ["Vaccinations"]


# ── a surface that only LISTS gaps must not invent one ───────────────────────
def test_unknown_proof_skips_the_check_rather_than_guessing():
    """has_proof=None means the caller could not cheaply find out. The booking
    gate always knows; a card page listing gaps may not, and must not accuse."""
    assert rg.missing_for(rg._ensure(GROUP), IN_DATE, BOOKING, has_proof=None) == []
    assert rg.missing_for(rg._ensure(GROUP), IN_DATE, BOOKING) == []


# ── the flag itself ──────────────────────────────────────────────────────────
def test_proof_is_meaningless_without_a_gate_and_is_dropped():
    f = rg.clean_field({"label": "Breed", "type": "text", "durability": "once",
                        "proof": True})
    assert f["proof"] is False


def test_proof_survives_on_a_real_gate():
    f = rg.clean_field({"label": "Vaccinations", "durability": "expires",
                        "gate": True, "proof": True})
    assert f["gate"] is True and f["proof"] is True


def test_the_editor_form_carries_the_flag():
    groups = rg.parse_editor_form(
        ["Dog"], ["Dogs"], ["many"], ["0"], ["Vaccinations"], ["date"],
        ["1"], ["expires"], ["1"], [""], f_proof=["1"])
    assert groups[0]["fields"][0]["proof"] is True


def test_the_editor_form_defaults_it_off():
    groups = rg.parse_editor_form(
        ["Dog"], ["Dogs"], ["many"], ["0"], ["Vaccinations"], ["date"],
        ["1"], ["expires"], ["1"], [""])
    assert groups[0]["fields"][0]["proof"] is False


# ── what the agent is told ───────────────────────────────────────────────────
def test_the_prompt_says_a_typed_date_is_not_evidence():
    block = rg.render_for_prompt([GROUP])
    assert "not evidence" in block


def test_the_customer_record_block_flags_the_missing_certificate():
    inst = [{"id": "i1", "group_label": "Dog", "name": "Bella", "data": IN_DATE}]
    known = rg.render_known([GROUP], inst, BOOKING, proof_ids=set())
    assert "certificate" in known
    assert "Bella" in known


def test_the_record_block_is_quiet_once_the_certificate_is_there():
    inst = [{"id": "i1", "group_label": "Dog", "name": "Bella", "data": IN_DATE}]
    known = rg.render_known([GROUP], inst, BOOKING, proof_ids={"i1"})
    assert "certificate" not in known
