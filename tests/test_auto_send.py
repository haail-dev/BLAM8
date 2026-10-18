"""Full-auto send.

Covers the reply-subject helper shared by the human-approve path (dashboard) and the
auto-send path (pipeline). The send-gating decision itself lives in
pipeline.draft_for_email and is exercised by the live check (it needs the
DB / mailbox / agent stack); the invariants it enforces are documented here:

  auto-send fires iff: insert was a fresh row (`created`) AND the caller opted in
  (`auto_send_ok`, set only by the automatic inbound paths — the Graph webhook and the
  Gmail poller) AND tenant_config.auto_send is on AND the email did NOT match an
  escalation keyword AND there is a from_address AND a reply actually exists (a
  knowledge hold or failed draft has nothing to send) AND the sender is not the
  tenant's own mailbox (self-mail would auto-reply into its own inbox and loop
  forever — it still drafts, it just never auto-sends).

Run: python -m pytest tests/ -q   (from the project root)
"""
from app.agent import build_system_prompt
from app.mailbox import reply_subject


def test_reply_subject_adds_prefix():
    assert reply_subject("Quote request") == "Re: Quote request"


def test_reply_subject_keeps_existing_re_any_case():
    assert reply_subject("Re: Quote request") == "Re: Quote request"
    assert reply_subject("RE: Quote request") == "RE: Quote request"
    assert reply_subject("re: quote") == "re: quote"


def test_reply_subject_does_not_double_prefix():
    assert reply_subject(reply_subject("Quote")) == "Re: Quote"


def test_reply_subject_blank_and_none():
    assert reply_subject(None) == "Re:"
    assert reply_subject("") == "Re:"
    assert reply_subject("   ") == "Re:"


# ── platform rule 1 follows the sending mode ─────────────────────────────────
# Caught live (2026-06-10): a full-auto reply told the customer "a human reviews
# every reply" — false in that mode. The prompt must match reality.
def test_prompt_claims_human_review_only_in_draft_mode():
    draft_mode = build_system_prompt({})
    assert "a human reviews and approves every reply" in draft_mode
    auto_mode = build_system_prompt({"auto_send": True})
    assert "a human reviews and approves" not in auto_mode
    assert "sent to the customer automatically" in auto_mode


def test_prompt_never_claims_actions_taken_in_either_mode():
    for cfg in ({}, {"auto_send": True}):
        assert "never claim an action has already been taken" in build_system_prompt(cfg)


def test_prompt_holds_the_reply_when_knowledge_is_missing():
    # Knowledge hold (Kris 2026-08-31): a deferred fact means NO reply is
    # written — the tokens wait for the knowledge, in every sending mode.
    for cfg in ({}, {"auto_send": True}):
        assert "leave `reply` completely empty" in build_system_prompt(cfg)


def test_prompt_forbids_thinking_aloud_in_missing():
    # Live 2026-08-31: a booking ask came back deferred with `missing` holding
    # the model's own deliberation ("...Actually this is not a knowledge gap,
    # I have the slots. Let me reconsider.") — the hold blocked the offer and
    # the gap card showed the monologue. Rule 5 now pins: `missing` names an
    # absent business fact, a scheduling ask is never a gap while the diary
    # tools are in hand, and a started deferral can be walked back.
    p = build_system_prompt({})
    assert "never your reasoning or planning" in p
    assert "never a knowledge gap while you have the diary tools" in p
    assert "set `deferred` back to false" in p
