"""Edit review (2026-09-29): a substantive change to a draft asks; a typo does not."""
import asyncio
from types import SimpleNamespace

from app import edit_review

DRAFT = ("Hi Sam,\n\nThanks for getting in touch. We can take Bella on Friday 3rd. "
         "Drop-off is between 8 and 10.\n\nBest wishes,\nHannah")


def test_verbatim_and_line_endings_ask_nothing():
    assert not edit_review.substantive(DRAFT, DRAFT)
    assert not edit_review.substantive(DRAFT, DRAFT.replace("\n", "\r\n"))


def test_typo_greeting_and_punctuation_ask_nothing():
    assert not edit_review.substantive(DRAFT, DRAFT.replace("Hi Sam", "Hello Sam"))
    assert not edit_review.substantive(DRAFT, DRAFT.replace("Friday 3rd.", "Friday 3rd!"))
    assert not edit_review.substantive(DRAFT, DRAFT.replace("touch", "tuch"))


def test_an_added_sentence_asks():
    edited = DRAFT.replace("Drop-off is between 8 and 10.",
                           "Drop-off is between 8 and 10. Late collection after 6pm costs £15.")
    assert edit_review.substantive(DRAFT, edited)


def test_a_reworded_answer_asks():
    edited = DRAFT.replace("We can take Bella on Friday 3rd.",
                           "Sorry, we are full on Friday 3rd but Saturday 4th is free.")
    assert edit_review.substantive(DRAFT, edited)


def test_an_owner_written_reply_never_asks():
    assert not edit_review.substantive("", DRAFT)
    assert not edit_review.substantive(None, DRAFT)
    assert not edit_review.substantive(DRAFT, "")


def test_a_failed_suggestion_shows_nothing(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(edit_review.llm, "create", boom)
    monkeypatch.setattr(edit_review.agent, "client", lambda: None)
    out = asyncio.run(edit_review.suggest("t1", "a", "b"))
    assert out["kind"] == "none"


def test_a_suggestion_comes_back_clean(monkeypatch):
    payload = ('{"kind":"knowledge","title":"Late collection","text":"Late collection after 6pm '
               'costs £15.","why":"You added the late collection fee."}')
    async def fake(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=payload)], usage=None)
    monkeypatch.setattr(edit_review.llm, "create", fake)
    monkeypatch.setattr(edit_review.agent, "client", lambda: None)
    monkeypatch.setattr(edit_review.usage, "record_anthropic", lambda *a, **k: None)
    out = asyncio.run(edit_review.suggest("t1", "a", "b"))
    assert out["kind"] == "knowledge" and "£15" in out["text"]


def _read(p):
    return open(p, encoding="utf-8").read()


def test_the_send_route_asks_only_after_a_substantive_edit_that_was_kept():
    src = _read("src/app/dashboard.py")
    assert "edit_review.substantive(entry.get(\"draft_reply\"), draft_reply)" in src
    assert "db.set_email_log_draft_original(" in src and 'review_out["ask"] = True' in src
    assert '"&learn=" + quote_plus(log_id)' in src
    # the ask rides the same "sent" branch: a skipped or failed send never asks
    assert src.index('if out == "sent":') < src.index('if review.get("ask"):')


def test_the_card_is_scrubbed_from_the_address_and_never_blocks():
    base = _read("src/app/templates/app/base.html")
    assert "u.searchParams.get('learn')" in base and "u.searchParams.delete('learn')" in base
    assert "s.kind === 'none'" in base and "data-edit-review" in base


def test_rules_append_deduped_to_the_named_list(monkeypatch):
    from app import dashboard
    store = {"never_do": "Never quote a firm price"}
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda t, client=None: dict(store))
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda t, cfg, client=None: store.update(cfg))
    assert dashboard._append_rule("t", "do_list", "Ask for the vaccination date")
    assert not dashboard._append_rule("t", "do_list", "Ask for the vaccination date")
    assert store["do_list"] == "Ask for the vaccination date"
    assert store["never_do"] == "Never quote a firm price"
