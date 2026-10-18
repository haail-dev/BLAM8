"""A confirmation is a moment, not a fixture.

Kris, 2026-09-01, looking at /app/settings?saved=1: "For some reason theres a
'Saved...' toast, permentaly on page?"

It was rendered from the query parameter, so it had no end — and the parameter
stayed in the address bar, so a refresh or a Back re-announced a save that had
already happened. It was also rendered halfway down the page, while the
redirect lands you at the top, so the one moment it existed for was the one
moment it could not be seen.
"""
import io
import re
import glob
import os

import pytest

from tests.test_settings_fold import client, _env      # noqa: F401  (fixture)


BASE = "src/app/templates/app/base.html"
SCRIPT = "src/app/static/flash.js"
BANNER = "bg-emerald-50 border border-emerald-200"


def _read(p):
    return io.open(p, encoding="utf-8").read()


def test_the_behaviour_is_loaded_app_wide():
    assert "/static/flash.js" in _read(BASE)


def test_the_url_is_scrubbed_so_a_refresh_cannot_re_announce_anything():
    js = _read(SCRIPT)
    assert "history.replaceState" in js
    assert "searchParams.delete" in js


def test_scrubbing_leaves_the_hash_alone():
    """Settings can be linked straight at a row (/app/settings#sending-mode
    from the inbox notice). Rebuilding the URL without the hash would break
    that link the moment a notice rode along with it."""
    js = _read(SCRIPT)
    rebuild = re.search(r"url\.pathname[^;]*", js)
    assert rebuild, "the URL is not rebuilt from parts"
    assert "url.hash" in rebuild.group(0)


def test_only_notice_parameters_are_scrubbed():
    """The dangerous version of this feature deletes something the page needs.
    `code`/`state` come back from OAuth, `token` signs a magic link, `ref` and
    `mailbox` address a record, `fix` opens a panel in draft review."""
    js = _read(SCRIPT)
    listed = re.search(r"var NOTICE = \[(.*?)\]", js, re.S)
    assert listed, "no NOTICE list"
    names = set(re.findall(r"'([a-z_]+)'", listed.group(1)))
    assert names, "NOTICE list is empty"
    for addressing in ("code", "state", "token", "ref", "mailbox", "fix",
                       "redraft", "prefill", "q", "from", "to"):
        assert addressing not in names, f"scrubbing '{addressing}' breaks a page"


def test_a_flash_has_to_look_like_a_notice():
    """The sweep that added these markers matched on colour, and colour is not
    enough: a thread's SENT REPLY bubble is emerald too, and both of them were
    marked — a conversation that deleted its own messages after seven seconds.

    A notice is a full-width strip: rounded-lg, px-4 py-3, text-sm. A chat
    bubble is rounded-2xl and max-w-[85%]. Anything wearing the marker has to
    be the strip."""
    for p in sorted(glob.glob("src/app/templates/app/*.html")):
        s = _read(p)
        for m in re.finditer(r"<div[^>]*data-flash[^>]*>", s):
            tag = m.group(0)
            where = f"{os.path.basename(p)}:{s.count(chr(10), 0, m.start()) + 1}"
            assert "rounded-lg" in tag, f"{where} is not a notice strip"
            assert "px-4 py-3" in tag, f"{where} is not a notice strip"
            assert "max-w-[" not in tag, f"{where} looks like a chat bubble"


def test_only_successes_fade_errors_stay_put():
    """An error is not 'what happened', it's 'this still needs you'. Nothing
    rose-coloured may carry the marker."""
    for p in sorted(glob.glob("src/app/templates/app/*.html")):
        s = _read(p)
        for m in re.finditer(r"<div[^>]*data-flash[^>]*>", s):
            tag = m.group(0)
            assert "rose" not in tag and "amber" not in tag, \
                f"{os.path.basename(p)} fades a warning away"


# ── The sweep ──────────────────────────────────────────────────────────────
NOTICE_COND = re.compile(
    r"(saved|msg|sched|portal|intake|resubscribed|sent|done|created|updated|"
    r"added|allocated|advanced|applied)", re.I)

# Emerald blocks that describe the page's CURRENT STATE rather than confirming
# something you just did. These must never disappear on a timer.
NOT_A_NOTICE = {
    # Both of these are message BUBBLES in the conversation, caught by a sweep
    # that matched on colour and on the word "sent". Marking them built a thread
    # that deleted its own replies after seven seconds.
    ("_thread.html", "e.status == 'sent'"),
    ("_thread.html", "latest and latest.status == 'sent'"),
    ("settings.html", "cfg.voice_learning_status == 'ready'"),
    ("shop_stock_in.html", ""),          # #ocr-done, filled by script
}


def _banners():
    for p in sorted(glob.glob("src/app/templates/app/*.html")):
        s = _read(p)
        for m in re.finditer(re.escape(BANNER), s):
            head = s[:m.start()]
            tags = list(re.finditer(r"\{%-?\s*(?:el)?if\s+([^%]+?)\s*-?%\}", head))
            cond = tags[-1].group(1).strip() if tags else ""
            div = s.rindex("<div ", 0, m.start())
            yield os.path.basename(p), cond, s[div:m.start()], head.count("\n") + 1


def test_every_confirmation_in_the_app_retires_itself():
    """The guard for the next page: a green 'saved' banner shown because of a
    redirect parameter has to say it is a flash, or it becomes furniture."""
    missing = []
    for name, cond, tag, line in _banners():
        if (name, cond) in NOT_A_NOTICE or (name, "") in NOT_A_NOTICE:
            continue
        if cond and NOTICE_COND.search(cond) and "data-flash" not in tag:
            missing.append(f"{name}:{line}")
    assert not missing, "confirmations that never leave: " + ", ".join(missing)


def test_the_standing_ones_are_left_alone():
    """A live status wearing the marker would vanish while still true."""
    for name, cond, tag, line in _banners():
        if (name, cond) in NOT_A_NOTICE or (name, "") in NOT_A_NOTICE:
            assert "data-flash" not in tag, f"{name}:{line} is a state, not a flash"


def test_the_exemptions_are_still_real():
    """An allowlist nobody re-checks becomes a hole."""
    seen = {(n, c) for n, c, _, _ in _banners()}
    for name, cond in NOT_A_NOTICE:
        assert any(n == name for n, _ in seen), f"{name} no longer has a banner"


# ── Where it lands ─────────────────────────────────────────────────────────
def test_the_confirmation_is_where_the_redirect_puts_you(client, monkeypatch):
    """Saving redirects to /app/settings?saved=1 and the browser lands at the
    top. The banner used to render inside the agent sheet, well below the fold
    — so it timed out unseen. It has to come before the page's first sheet."""
    _env(monkeypatch)
    html = client.get("/app/settings?saved=1").text
    assert "Saved. New drafts will use these instructions." in html
    banner = html.index("Saved. New drafts will use these instructions.")
    workspace = html.index(">Workspace<")
    assert banner < workspace, "the confirmation is below the first sheet again"


def test_the_page_says_nothing_when_nothing_happened(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/settings").text
    assert "Saved. New drafts will use these instructions." not in html


def test_portal_errors_stay_beside_the_field_they_are_about(client, monkeypatch):
    """Only the confirmation moves. 'That web address is already taken' belongs
    next to the box you have to change, not at the top of the page."""
    _env(monkeypatch)
    html = client.get("/app/settings?portal=slug_taken").text
    err = html.index("already taken")
    form = html.index('action="/app/settings/portal"')
    assert form - err < 4000, "the portal error drifted away from its form"
