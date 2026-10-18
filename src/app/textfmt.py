"""Plain-text normaliser for outgoing email bodies.

The drafting model naturally reaches for Markdown (`**bold**`, `# headings`, `- `/`* `
bullets, `[text](url)` links). Our emails are sent as **text/plain** (src/app/mailbox.py →
`contentType: "Text"` / `EmailMessage.set_content`), so that Markdown reaches the customer as
literal asterisks/hashes. `to_plain_text` strips the common Markdown emphasis/structure so the
stored draft — and therefore the approval UI AND the sent email — read cleanly.

Deliberately conservative: it removes formatting *markers* only, never content, and leaves
ambiguous cases (single underscores in identifiers/emails, asterisks used as maths) alone. If the
input has no Markdown, the output is byte-identical.
"""
from __future__ import annotations

import re

# Inline emphasis/markup — markers wrapping a run of text. Order matters: the double-marker
# forms run before the single-marker ones so `**x**` isn't half-eaten by the `*x*` pass.
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD_ALT = re.compile(r"__(.+?)__", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_CODE = re.compile(r"`([^`]+)`")
# A single * pair used for emphasis: not touching word chars or another * on the outside, and
# the inner run doesn't start/end with whitespace — so "2 * 3" and bullet stars are left alone.
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
# [label](url) → "label (url)"; bare autolinks <url> → url
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")          # "## Title" → "Title"
_BULLET = re.compile(r"^(\s*)[*+]\s+")               # "* item" / "+ item" → "- item"
_BLANKS = re.compile(r"\n{3,}")                      # collapse 3+ blank lines to 2


def to_plain_text(text: str | None) -> str:
    """Strip common Markdown markers from an email body, keeping all content. Pure."""
    if not text:
        return text or ""
    out_lines = []
    for line in text.split("\n"):
        line = _HEADING.sub("", line)
        line = _BULLET.sub(r"\1- ", line)
        line = _BOLD.sub(r"\1", line)
        line = _BOLD_ALT.sub(r"\1", line)
        line = _STRIKE.sub(r"\1", line)
        line = _LINK.sub(r"\1 (\2)", line)
        line = _AUTOLINK.sub(r"\1", line)
        line = _CODE.sub(r"\1", line)
        line = _ITALIC.sub(r"\1", line)
        out_lines.append(line)
    return _BLANKS.sub("\n\n", "\n".join(out_lines))
