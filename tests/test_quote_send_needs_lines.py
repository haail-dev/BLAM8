"""A quote with nothing on it is never sent (live, 1 Oct 2026, F28: a quote whose
lines failed to save kept its totals and was emailed to the customer, who opened
a total with no rows)."""
import asyncio

import pytest

from app import db, mailbox, quote_send


def _env(monkeypatch, lines):
    sent = []
    monkeypatch.setattr(db, "list_quote_lines", lambda qid, tid, client=None: lines)

    async def send_mail(*a, **k):
        sent.append(a)
    monkeypatch.setattr(mailbox, "send_mail", send_mail)
    return sent


def test_a_quote_with_no_lines_is_refused_before_anything_is_sent(monkeypatch):
    sent = _env(monkeypatch, [])
    with pytest.raises(quote_send.QuoteHasNoLines):
        asyncio.run(quote_send.send_quote_email("t1", {"id": "q1", "customer_email": "a@b.example"}))
    assert sent == []


def test_a_quote_whose_only_lines_are_unselected_is_refused_too(monkeypatch):
    sent = _env(monkeypatch, [{"description": "Extra", "amount": 18, "is_optional": True, "is_selected": False}])
    with pytest.raises(quote_send.QuoteHasNoLines):
        asyncio.run(quote_send.send_quote_email("t1", {"id": "q1", "customer_email": "a@b.example"}))
    assert sent == []


def test_the_quote_page_says_why_it_was_not_sent():
    from pathlib import Path
    page = Path("src/app/templates/app/quote.html").read_text(encoding="utf-8")
    assert "msg == 'nolines'" in page or "msg == 'nolines'" in page
    assert "This quote has no lines, so it wasn't sent." in page
