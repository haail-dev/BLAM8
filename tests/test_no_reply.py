"""The pipeline must never draft a reply to a system no-reply / do-not-reply sender."""
import pytest

from app.pipeline import is_no_reply_address


@pytest.mark.parametrize("addr", [
    "no-reply@accounts.google.com",
    "noreply@github.com",
    "no_reply@example.com",
    "no.reply@example.com",
    "google-noreply@google.com",
    "donotreply@bank.co.uk",
    "do-not-reply@service.com",
    "NoReply@LOUD.COM",
    "notifications-noreply@stripe.com",
    # bounce / delivery-report senders — replying mails a server, never a human
    "mailer-daemon@googlemail.com",
    "MAILER-DAEMON@mx1.example.net",
    "mailerdaemon@qmail.example.com",
    "postmaster@theirdomain.com",
    "Postmaster@outlook.com",
])
def test_no_reply_addresses_match(addr):
    assert is_no_reply_address(addr) is True


@pytest.mark.parametrize("addr", [
    "kris.deane93@gmail.com",
    "sarah@thornbeck.co.uk",
    "info@customer.com",
    "replies@agency.com",          # 'reply' but not 'noreply' — a real human box
    "thepostmasterpub@gmail.com",  # a business CALLED The Postmaster — exact match only
    "postmaster.jones@gmail.com",  # ...but separators strip to 'postmasterjones', still human
    "",
    None,
    "not-an-email",
])
def test_real_senders_do_not_match(addr):
    assert is_no_reply_address(addr) is False
