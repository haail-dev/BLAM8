"""Phase 4 — per-thread conversation memory.

Unit coverage for the pure helpers that turn our stored email_log rows into a clean
multi-turn conversation for the drafting agent (see docs/architecture/phase4-conversation-memory.md).
These functions are deliberately conservative: when quote-stripping would empty a body
they fall back to the original, so genuinely-new content is never dropped.

Run: python -m pytest tests/ -q   (from the project root)
"""
from app.agent import strip_escalation, strip_quoted, thread_messages


# ── strip_quoted ─────────────────────────────────────────────────────────────
def test_strip_quoted_gmail_apple():
    body = (
        "Thanks, that works for me.\n\n"
        "On Mon, Jan 1, 2024 at 10:00 AM John Doe <john@x.com> wrote:\n"
        "> Are you free Monday?\n> Let me know."
    )
    assert strip_quoted(body) == "Thanks, that works for me."


def test_strip_quoted_outlook_original_message():
    body = (
        "Yes please proceed.\n\n"
        "-----Original Message-----\n"
        "From: John\nSubject: Quote\n\nHere is the quote."
    )
    assert strip_quoted(body) == "Yes please proceed."


def test_strip_quoted_outlook_from_header_block():
    body = (
        "See my answer below.\n\n"
        "From: John Doe <john@x.com>\n"
        "Sent: Monday, January 1, 2024 10:00 AM\n"
        "To: Me\nSubject: Quote\n\nWhat is the price?"
    )
    assert strip_quoted(body) == "See my answer below."


def test_strip_quoted_outlook_underscore_rule():
    body = (
        "Confirmed for 3pm.\n\n"
        "___________________________\n"
        "This email and any attachments are confidential."
    )
    assert strip_quoted(body) == "Confirmed for 3pm."


def test_strip_quoted_caret_quoted_run():
    body = "No problem at all.\n\n> previous line 1\n> previous line 2"
    assert strip_quoted(body) == "No problem at all."


def test_strip_quoted_quote_only_falls_back_to_original():
    body = "> Are you free Monday?\n> Let me know."
    assert strip_quoted(body) == body  # stripping would empty it → keep original


def test_strip_quoted_plain_body_unchanged():
    body = "Hi, can you send me a quote for 2 pallets to Leeds?"
    assert strip_quoted(body) == body


def test_strip_quoted_is_conservative_about_bare_from_line():
    # "From:" without a following Sent/Date/To/Subject header is NOT a quote delimiter.
    body = "From: my point of view this looks great.\nLet me know if that works."
    assert strip_quoted(body) == body


def test_strip_quoted_drops_trailing_mobile_signature():
    assert strip_quoted("Sounds good.\n\nSent from my iPhone") == "Sounds good."


def test_strip_quoted_empty_and_whitespace():
    assert strip_quoted("") == ""
    assert strip_quoted("   ") == "   "


# Gmail's plain-text part wraps the attribution at ~78 columns, so "wrote:" lands
# on its own line (live 2026-09-20: the kennels film's thread showed the rig's own
# reply, quoted back, as the customer's message).
_GMAIL_WRAPPED_QUOTE = (
    "On Sun, 20 Sept 2026 at 09:55, Test business <business.blam8@gmail.com>\n"
    "wrote:\n\n"
    "> Hi Hannah,\n>\n> We do have space that weekend — lovely!\n>\n"
    "> Boarding is £30.50 per night for one dog, so 3 nights (Friday 9\n"
    "> October to Monday 12 October) comes to £91.50.\n"
)


def test_strip_quoted_gmail_wrapped_attribution():
    body = "Biscuit is a four-year-old spaniel.\n\n" + _GMAIL_WRAPPED_QUOTE
    assert strip_quoted(body) == "Biscuit is a four-year-old spaniel."


def test_strip_quoted_gmail_attribution_wrapped_over_three_lines():
    body = (
        "Yes please.\n\n"
        "On Sun, 20 Sept 2026 at 09:55, A Very Long Kennels Business Name Limited\n"
        "<a.very.long.address@example-domain.co.uk>\n"
        "wrote:\n\n> Shall I hold the weekend?\n"
    )
    assert strip_quoted(body) == "Yes please."


def test_strip_quoted_keeps_attachment_marker_after_the_quote():
    # The mail readers append "[Attachments: …]" AFTER the quoted chain, so the
    # marker used to defeat the trailing-quote cut and the drafter lost nothing but
    # the thread showed the whole chain. The marker survives; the quote goes.
    body = ("Card attached as asked.\n\n" + _GMAIL_WRAPPED_QUOTE
            + "\n[Attachments: vaccination-card.pdf]")
    assert strip_quoted(body) == "Card attached as asked.\n\n[Attachments: vaccination-card.pdf]"


def test_strip_quoted_file_only_reply_is_just_the_marker():
    # A reply that is only the attachment: the marker, never the quoted chain.
    body = _GMAIL_WRAPPED_QUOTE + "\n\n[Attachments: vaccination-card.pdf]"
    assert strip_quoted(body) == "[Attachments: vaccination-card.pdf]"


def test_strip_quoted_keeps_words_typed_under_the_quote():
    # Bottom-posted: the customer's reply sits under the '>' block.
    body = (_GMAIL_WRAPPED_QUOTE + "\nBiscuit is a spaniel, jabs to June 2027.\n\n"
            "[Attachments: vaccination-card.pdf]")
    assert strip_quoted(body) == ("Biscuit is a spaniel, jabs to June 2027.\n\n"
                                  "[Attachments: vaccination-card.pdf]")


def test_strip_quoted_html_flattened_quote_is_cut_whole():
    # An HTML-only client: the blockquote flattens to un-prefixed lines, so nothing
    # after the attribution can be told apart from the quote — cut it all.
    body = ("Thanks!\n\nOn Sun, 20 Sept 2026 at 09:55, Test business "
            "< business.blam8@gmail.com > wrote:\n Hi Hannah, \n We do have space")
    assert strip_quoted(body) == "Thanks!"


def test_strip_quoted_prose_line_starting_with_on_is_not_folded_into_attribution():
    body = "On Saturday we'd like to drop him off at 9.\n\n" + _GMAIL_WRAPPED_QUOTE
    assert strip_quoted(body) == "On Saturday we'd like to drop him off at 9."


def test_strip_quoted_mobile_signature_before_attachment_marker():
    assert strip_quoted("Sounds good.\n\nSent from my iPhone\n\n[Attachments: a.pdf]") == (
        "Sounds good.\n\n[Attachments: a.pdf]")


# ── strip_escalation ─────────────────────────────────────────────────────────
def test_strip_escalation_removes_leading_marker():
    text = "[⚠ ESCALATION — matched 'urgent', needs review]\n\nHere is the reply."
    assert strip_escalation(text) == "Here is the reply."


def test_strip_escalation_noop_without_marker():
    assert strip_escalation("No marker here.") == "No marker here."


# ── thread_messages ──────────────────────────────────────────────────────────
def test_thread_messages_alternation_and_sent_only_assistant():
    rows = [
        {"id": "1", "kind": "inbound", "body_in": "What's your price for X?", "draft_reply": "It's £50.", "status": "sent"},
        {"id": "2", "kind": "inbound", "body_in": "Great, deliver Monday?", "draft_reply": "draft only", "status": "pending"},
    ]
    msgs = thread_messages(rows, "Actually make it Tuesday.")
    assert msgs == [
        {"role": "user", "content": "What's your price for X?"},
        {"role": "assistant", "content": "It's £50."},
        {"role": "user", "content": "Great, deliver Monday?"},          # pending → no assistant turn
        {"role": "user", "content": "Actually make it Tuesday."},
    ]


def test_thread_messages_skipped_status_has_no_assistant_turn():
    rows = [{"id": "1", "kind": "inbound", "body_in": "Hello?", "draft_reply": "ignored", "status": "skipped"}]
    assert thread_messages(rows, "Still there?") == [
        {"role": "user", "content": "Hello?"},
        {"role": "user", "content": "Still there?"},
    ]


def test_thread_messages_strips_escalation_from_assistant_turn():
    rows = [{
        "id": "1",
        "kind": "inbound", "body_in": "Help, this is urgent!",
        "draft_reply": "[⚠ ESCALATION — matched 'urgent', needs review]\n\nWe're on it.",
        "status": "sent",
    }]
    msgs = thread_messages(rows, "Any update?")
    assert msgs[1] == {"role": "assistant", "content": "We're on it."}


def test_thread_messages_drops_leading_assistant_turn():
    # Keep the pinned guard name: user-first remains required, but a sent
    # receipt now survives behind a context marker instead of being discarded.
    # Keep the sent receipt even when it predates all customer turns in the cap.
    rows = [{"id": "1", "kind": "owner_message", "body_in": "",
             "draft_reply": "Earlier outbound note.", "status": "sent"}]
    msgs = thread_messages(rows, "Hello again.")
    assert msgs[0]["role"] == "user"
    assert "not customer requests or authorisation" in msgs[0]["content"]
    assert msgs[1:] == [
        {"role": "assistant", "content": "Earlier outbound note."},
        {"role": "user", "content": "Hello again."},
    ]


def test_thread_messages_new_sender_single_user_turn():
    assert thread_messages([], "Brand new enquiry.") == [
        {"role": "user", "content": "Brand new enquiry."},
    ]


def test_thread_messages_de_quotes_history_bodies():
    rows = [{
        "id": "1",
        "kind": "inbound", "body_in": "Yes that's fine.\n\nOn Mon Jan 1 John wrote:\n> original question",
        "draft_reply": "Great, see you then.",
        "status": "sent",
    }]
    msgs = thread_messages(rows, "One more thing.")
    assert msgs[0] == {"role": "user", "content": "Yes that's fine."}


def test_thread_messages_redraft_excludes_current_row_no_duplication():
    # redraft passes history with the row being regenerated removed; the current body is
    # supplied separately, so it must appear exactly once (the final user turn).
    all_rows = [
        {"id": "A", "kind": "inbound", "body_in": "First question?", "draft_reply": "First answer.", "status": "sent"},
        {"id": "B", "kind": "inbound", "body_in": "Second question?", "draft_reply": "draft B", "status": "pending"},
    ]
    log_id = "B"
    history = [r for r in all_rows if r["id"] != log_id]
    msgs = thread_messages(history, all_rows[1]["body_in"])
    assert msgs == [
        {"role": "user", "content": "First question?"},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "Second question?"},
    ]
    assert sum(m["content"] == "Second question?" for m in msgs) == 1


def test_thread_messages_skips_retention_redacted_rows():
    # After the 90-day purge a row's body/draft becomes "[redacted]"; those must not
    # leak into the model context as turns (regression guard for the purge interaction).
    rows = [
        {"id": "1", "kind": "inbound", "body_in": "[redacted]", "draft_reply": "[redacted]", "status": "sent"},
        {"id": "2", "kind": "inbound", "body_in": "Real recent question?", "draft_reply": "Real answer.", "status": "sent"},
    ]
    msgs = thread_messages(rows, "Latest.")
    assert {"role": "user", "content": "[redacted]"} not in msgs
    assert {"role": "assistant", "content": "[redacted]"} not in msgs
    assert msgs == [
        {"role": "user", "content": "Real recent question?"},
        {"role": "assistant", "content": "Real answer."},
        {"role": "user", "content": "Latest."},
    ]


def test_thread_messages_respects_max_rows_cap():
    from app.agent import THREAD_CONTEXT_MAX_ROWS
    rows = [
        {"id": str(i), "kind": "inbound", "body_in": f"msg {i}", "draft_reply": f"reply {i}", "status": "sent"}
        for i in range(THREAD_CONTEXT_MAX_ROWS + 5)
    ]
    msgs = thread_messages(rows, "latest")
    # newest kept: oldest surviving inbound is row index 5 (first 5 dropped by the cap).
    assert msgs[0] == {"role": "user", "content": "msg 5"}
    assert msgs[-1] == {"role": "user", "content": "latest"}
    assert len(msgs) == THREAD_CONTEXT_MAX_ROWS * 2 + 1
