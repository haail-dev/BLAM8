"""The thumbs-up says what it's doing (Kris 2026-09-24: a send sat behind a
spinner that read as stuck). "Sending…" on tap, "Sent ✓" when it landed."""


def _read(p):
    return open(p, encoding="utf-8").read()


def test_the_draft_line_says_sending_on_tap():
    tpl = _read("src/app/templates/app/_thread.html")
    # the stage draft (its email attach label, or the SMS label) and the booking card
    assert tpl.count("data-send-status") == 3
    js = _read("src/app/static/nav.js")
    assert "form.querySelector('[data-send-status]')" in js
    assert "'Sending — with the deposit…' : 'Sending…'" in js


def test_sent_is_said_only_when_the_email_went():
    src = _read("src/app/dashboard.py")
    assert 'if out == "sent":' in src and '"sent=1"' in src
    base = _read("src/app/templates/app/base.html")
    assert "u.searchParams.get('sent') !== '1'" in base and "data-sent-toast" in base
    assert "u.searchParams.delete('sent')" in base       # a refresh never says it again
