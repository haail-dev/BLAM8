"""Reading the card (docs/plan-document-backed-facts.md slice 4, 2026-09-22).

Kris: "Doc still aren't being auto labelled." A vet card arrived, was kept on
the record — and nothing read it: no label, and the new vaccination expiry sat
inside the PDF where the gate could not see it. Now a kept PDF's text is read,
and one call labels the document, links it to its dependant, and reads the
facts it states through the SAME gate as an email: confidence, the echo check
(against the document's own words), and "You confirm" turning a sure read into
a chip for the owner.

Run: python -m pytest tests/test_document_read.py -q
"""
import json
from types import SimpleNamespace

import pytest

from app import dependants_extract as dx
from app import documents
from app import requirement_groups as rg
from app import usage

CARD_TEXT = ("Fosse Way Veterinary Practice\nVaccination record\nPatient Alfie\n"
             "Owner Claire Dunmore\nVaccine Given Next due\n"
             "DHP (distemper, hepatitis, parvo) 22 September 2026 22 September 2027\n"
             "Kennel cough (intranasal) 22 September 2026 22 September 2027\n")


def _dog(set_by="customer"):
    return rg.clean_group({"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "fields": [
        {"label": "Vaccinations", "type": "date", "durability": "expires", "gate": True,
         "required": True, "set_by": set_by},
        {"label": "Breed", "type": "text", "durability": "once"}]})


ALFIE = {"id": "i-alfie", "group_id": "g-dog", "contact_id": "c-1", "name": "Alfie",
         "data": {"breed": "Labradoodle", "vaccinations": "2026-10-25"}, "field_meta": {}}


def _pdf(text: str) -> bytes:
    from fpdf import FPDF
    pdf = FPDF(format="A5")
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)
    for line in text.splitlines():
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ── the text layer ───────────────────────────────────────────────────────────
def test_a_pdf_is_read_and_anything_else_is_not():
    got = documents.document_text("k1-alfie.pdf", "application/pdf", _pdf(CARD_TEXT))
    assert "Patient Alfie" in got and "22 September 2027" in got
    # by extension when the mailbox gave no type
    assert "Alfie" in documents.document_text("card.PDF", None, _pdf(CARD_TEXT))
    assert documents.document_text("photo.jpg", "image/jpeg", b"\xff\xd8\xff") == ""
    assert documents.document_text("broken.pdf", "application/pdf", b"not a pdf at all") == ""
    assert documents.document_text("empty.pdf", "application/pdf", b"") == ""


# ── the prompt ───────────────────────────────────────────────────────────────
def test_an_expiry_field_is_read_as_the_day_it_lapses_not_the_day_it_was_given():
    p = dx.build_doc_prompt([_dog()], [ALFIE], [{"filename": "k1-alfie.pdf", "text": CARD_TEXT}])
    assert "key=vaccinations" in p and "the date it lapses" in p and "never the date it was given" in p
    assert "Dog 'Alfie'" in p and "Document 0 — file 'k1-alfie.pdf'" in p and "Next due" in p
    # the email extractor gets the same instruction
    assert "the date it lapses" in dx.build_user_prompt([_dog()], [], "s", "b", "Claire")


def test_a_tenant_with_no_dependants_still_gets_its_documents_labelled():
    p = dx.build_doc_prompt([], [], [{"filename": "policy.pdf", "text": "Insurance certificate"}])
    assert "only the labels" in p and "Insurance certificate" in p


# ── labels and links ─────────────────────────────────────────────────────────
def test_label_plan_labels_links_and_never_overwrites_the_owner():
    docs = [{"id": "d0", "label": None, "instance_id": None},
            {"id": "d1", "label": "Alfie's card (Kris)", "instance_id": None},
            {"id": "d2", "label": "", "instance_id": "i-other"}]
    read = [{"index": 0, "label": "vaccination record.", "dependant": "alfie"},
            {"index": 1, "label": "Vaccination record", "dependant": "Alfie"},
            {"index": 2, "label": "Insurance certificate", "dependant": "Alfie"},
            {"index": 7, "label": "Out of range", "dependant": ""},
            {"index": "x", "label": "Junk", "dependant": ""}]
    assert dx.label_plan(docs, read, [ALFIE]) == [
        {"id": "d0", "patch": {"label": "Vaccination record", "instance_id": "i-alfie"}},
        {"id": "d1", "patch": {"instance_id": "i-alfie"}},          # the owner's label stands
        {"id": "d2", "patch": {"label": "Insurance certificate"}},  # an existing link stands
    ]


def test_a_dependant_the_record_does_not_have_never_invents_a_link():
    docs = [{"id": "d0", "label": None, "instance_id": None}]
    assert dx.label_plan(docs, [{"index": 0, "label": "Vaccination record", "dependant": "Biscuit"}],
                         [ALFIE]) == [{"id": "d0", "patch": {"label": "Vaccination record"}}]


# ── the whole read, with the model faked ─────────────────────────────────────
def _wire(monkeypatch, group, reply: dict):
    seen = {"updates": [], "writes": [], "chips": [], "events": [], "calls": 0}

    async def fake_create(**kw):
        seen["calls"] += 1
        seen["operation"] = kw.get("operation")
        seen["prompt"] = kw["messages"][0]["content"]
        return SimpleNamespace(model="m", usage=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(reply))])
    monkeypatch.setattr(dx.llm, "create", fake_create)
    monkeypatch.setattr(dx.agent, "client", lambda: None)
    monkeypatch.setattr(dx.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(dx.db, "list_requirement_groups", lambda t, client=None: [group])
    monkeypatch.setattr(dx.db, "list_requirement_instances", lambda t, cid, client=None: [ALFIE])
    monkeypatch.setattr(dx.db, "update_document",
                        lambda doc_id, t, patch, client=None: seen["updates"].append((doc_id, patch)))
    monkeypatch.setattr(dx.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta=None, client=None:
                        seen["writes"].append((name, data)) or {"id": "x"})
    monkeypatch.setattr(dx.db, "crm_insert_field_suggestion",
                        lambda t, **kw: seen["chips"].append(kw) or True)
    monkeypatch.setattr(dx.db, "log_agent_event",
                        lambda t, kind, summary, **kw: seen["events"].append((kind, summary)))
    return seen


READ = {"documents": [{"index": 0, "label": "Vaccination record", "dependant": "Alfie"}],
        "instances": [{"group": "Dog", "name": "Alfie", "confidence": 0.95, "fields": [
            {"key": "vaccinations", "value": "2027-09-22", "confidence": 0.95}]}]}
KEPT = [{"id": "d0", "filename": "k1-alfie.pdf", "label": None, "instance_id": None,
         "contact_id": "c-1", "text": CARD_TEXT}]


@pytest.mark.asyncio
async def test_you_confirm_turns_the_cards_new_date_into_a_chip_for_the_owner(monkeypatch):
    seen = _wire(monkeypatch, _dog(set_by="owner"), READ)
    out = await dx.read_documents("t1", "c-1", KEPT, email_log_id="log-1", contact="claire@x.co")
    assert seen["operation"] == "document_read" and seen["calls"] == 1
    assert seen["updates"] == [("d0", {"label": "Vaccination record", "instance_id": "i-alfie"})]
    assert seen["writes"] == []                                   # NOT written: the owner decides
    assert [(c["record_id"], c["field_key"], c["value"]) for c in seen["chips"]] == \
        [("i-alfie", "vaccinations", "2027-09-22")]
    assert out == {"labelled": 1, "written": 0, "suggested": 1, "skipped": 0}
    assert seen["events"] == [("document_read",
                               "Read Vaccination record: 1 waiting for you to confirm")]


@pytest.mark.asyncio
async def test_a_documents_read_always_asks_the_owner_with_no_setting_needed(monkeypatch):
    """Kris 2026-09-22: the card should arrive "asking for a confirmation to use
    it to update info" — the owner should not have to find a per-field setting
    first. So even on a customer-set field, a document's read is a chip."""
    seen = _wire(monkeypatch, _dog(set_by="customer"), READ)
    out = await dx.read_documents("t1", "c-1", KEPT, email_log_id="log-1")
    assert seen["writes"] == []
    assert [(c["record_id"], c["value"]) for c in seen["chips"]] == [("i-alfie", "2027-09-22")]
    assert out["suggested"] == 1 and out["written"] == 0


def test_from_a_document_new_facts_fill_in_and_only_a_change_asks():
    """Kris 2026-09-22: "It should only offer to change info that has actually
    changed, if it's new info then it's not an issue." """
    dog = rg.clean_group({"id": "g-dog", "label": "Dog", "fields": [
        {"label": "Vaccinations", "type": "date", "durability": "expires", "gate": True},
        {"label": "Breed", "type": "text", "durability": "once"},
        {"label": "Date of birth", "type": "text", "durability": "once"}]})
    read = [{"group": "Dog", "name": "Alfie", "confidence": 0.95, "fields": [
        {"key": "vaccinations", "value": "2027-09-22", "confidence": 0.95},   # changed → ask
        {"key": "breed", "value": "Labradoodle", "confidence": 0.95},         # the same → nothing
        {"key": "date_of_birth", "value": "2024-04-22", "confidence": 0.95}]}]  # new → fill in
    steps = dx.plan([dog], [ALFIE], read, text=CARD_TEXT + " 22 April 2024", confirm_all=True)
    assert [(s["action"], s["data"]) for s in steps] == [
        ("write", {"date_of_birth": "2024-04-22"}),
        ("suggest", {"vaccinations": "2027-09-22"})]


def test_an_emails_read_on_a_customer_field_still_writes_as_before():
    """Only documents changed: a sure read from an email body on a field the
    customer may set still lands on the record without a chip."""
    steps = dx.plan([_dog(set_by="customer")], [ALFIE], READ["instances"], text=CARD_TEXT)
    assert [s["action"] for s in steps] == ["write"]


# ── the update sits on its document ──────────────────────────────────────────
CHIP = {"id": "s1", "kind": "field", "email_log_id": "log-2", "record_id": "i-alfie",
        "who": "Alfie", "label": "Vaccinations", "value_text": "2027-09-22", "replaces": "2026-10-25"}


def test_a_documents_update_is_shown_on_it_and_leaves_the_dependants_list():
    docs = [{"id": "d-new", "email_log_id": "log-2", "instance_id": "i-alfie", "label": "Vaccination record",
             "filename": "k1-alfie.pdf"},
            {"id": "d-old", "email_log_id": "log-0", "instance_id": "i-alfie", "label": "vaccination record ",
             "filename": "alfie-2025.pdf"}]
    other = {**CHIP, "id": "s2", "email_log_id": "log-9"}          # read from a plain email
    out, left = documents.with_updates(docs, [CHIP, other])
    assert [u["id"] for u in out[0]["updates"]] == ["s1"]
    assert out[0]["replaces"]["id"] == "d-old"                       # the new version of it
    assert out[1]["updates"] == [] and out[1]["replaces"] is None     # the oldest replaces nothing
    assert [c["id"] for c in left] == ["s2"]


def test_a_chip_about_another_dependant_is_not_claimed_by_a_linked_document():
    docs = [{"id": "d", "email_log_id": "log-2", "instance_id": "i-other", "label": "Vaccination record"}]
    out, left = documents.with_updates(docs, [CHIP])
    assert out[0]["updates"] == [] and left == [CHIP]
    # an unlinked document from the same email does claim it
    out, left = documents.with_updates([{**docs[0], "instance_id": None}], [CHIP])
    assert out[0]["updates"] == [CHIP] and left == []


@pytest.mark.asyncio
async def test_a_date_the_card_never_prints_is_not_a_read(monkeypatch):
    invented = {**READ, "instances": [{"group": "Dog", "name": "Alfie", "confidence": 0.95, "fields": [
        {"key": "vaccinations", "value": "2028-03-14", "confidence": 0.99}]}]}
    seen = _wire(monkeypatch, _dog(set_by="customer"), invented)
    out = await dx.read_documents("t1", "c-1", KEPT)
    assert seen["writes"] == [] and seen["chips"] == []
    assert out["labelled"] == 1                                    # the label still lands


@pytest.mark.asyncio
async def test_nothing_readable_costs_nothing(monkeypatch):
    seen = _wire(monkeypatch, _dog(), READ)
    assert await dx.read_documents("t1", "c-1", [{"id": "d0", "filename": "x.jpg"}]) is None
    assert await dx.read_documents("t1", "c-1", []) is None
    assert seen["calls"] == 0


@pytest.mark.asyncio
async def test_a_model_failure_never_fails_the_email(monkeypatch):
    seen = _wire(monkeypatch, _dog(), READ)

    async def boom(**kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(dx.llm, "create", boom)
    assert await dx.read_documents("t1", "c-1", KEPT) is None
    assert seen["updates"] == []


# ── the pipeline hands the kept files over ───────────────────────────────────
@pytest.mark.asyncio
async def test_keep_attachments_reads_what_it_kept(monkeypatch):
    from app import pipeline
    calls = []

    async def fake_save(tenant_id, **kw):
        return [{"id": "d0", "contact_id": "c-1", "text": CARD_TEXT},
                {"id": "d1", "contact_id": "c-1"}]                  # a photo: kept, not read

    async def fake_read(tenant_id, contact_id, docs, **kw):
        calls.append((contact_id, [d["id"] for d in docs], kw))
    monkeypatch.setattr(documents, "save_email_attachments", fake_save)
    monkeypatch.setattr(dx, "read_documents", fake_read)
    await pipeline.keep_attachments("t1", "m-1", "log-1",
                                    {"from_address": "claire@x.co", "subject": "Alfie",
                                     "body": "card attached", "attachments": [{"name": "k1-alfie.pdf"}]},
                                    mailbox_id=None)
    assert calls == [("c-1", ["d0"], {"email_log_id": "log-1", "contact": "claire@x.co"})]


@pytest.mark.asyncio
async def test_no_readable_file_means_no_read(monkeypatch):
    from app import pipeline
    calls = []

    async def fake_save(tenant_id, **kw):
        return [{"id": "d1", "contact_id": "c-1"}]

    async def fake_read(*a, **k):
        calls.append(a)
    monkeypatch.setattr(documents, "save_email_attachments", fake_save)
    monkeypatch.setattr(dx, "read_documents", fake_read)
    await pipeline.keep_attachments("t1", "m-1", "log-1",
                                    {"from_address": "c@x.co", "attachments": [{"name": "a.jpg"}]},
                                    mailbox_id=None)
    assert calls == []


def test_the_read_is_metered_as_inbound_email():
    assert usage.SOURCE_BY_OPERATION["document_read"] == "inbound_email"
