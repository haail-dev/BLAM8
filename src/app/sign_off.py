"""The configured email sign-off, applied by code (14 Oct 2026, Kris: "it needs to
become deterministic"). The model is told the sign-off is added for it and must not
write one; this module normalises the stored value, removes a closing the model wrote
anyway, and appends the configured text once. Email only: SMS keeps its own register.

Pure functions, no I/O. The single source stays the sign-off the business already
gave at onboarding (tenant_config.sign_off, with the per-mailbox override merged by
db.effective_config); nothing here stores or gathers anything.

Only the append is a guarantee. Removing a closing the model wrote is a courtesy
that errs one way: when in doubt the model's text is KEPT, because a visible doubled
closing is cosmetic and deleting something written for the customer is not.
"""
import re

# A closing phrase (matched case-insensitively wherever it is used).
_CLOSING_WORDS = (
    r"kind regards|kindest regards|best regards|warm regards|warmest regards|regards|"
    r"with kind regards|with best wishes|best wishes|warm wishes|"
    r"many thanks|thanks so much|thank you so much|thanks very much|thank you very much|"
    r"thanks again|thank you again|thanks|thank you|with thanks|cheers|"
    r"yours sincerely|yours faithfully|yours truly|sincerely|warmly|all the best|best|"
    r"take care|speak soon|talk soon")
# A short name after a closing: up to three capitalised tokens ("Sam", "Sam Jones",
# "The Example Team"). Case-sensitive on purpose, so a sentence never looks like one.
_SHORT_NAME = (r"[A-Z][\w'’.&-]*"
               r"(?:[ \t]+[A-Z&][\w'’.&-]*){0,2}")
# A whole line that is a closing: "Thanks", "Kind regards,", or one line with a name
# after a comma ("Thanks, Sam", "Best, Sam Jones").
_CLOSING_LINE = re.compile(r"^(?i:" + _CLOSING_WORDS + r")(?:[,!.]?|,[ \t]+" + _SHORT_NAME + r")$")
# " / " joining two alternative closings (one learned value had this): the part after
# the slash must be a bare closing LINE ("Kind regards,"), so "Alex / Best Price
# Removals" or "Sam / best@example.invalid" are never cut.
_ALTERNATIVE = re.compile(
    r"\s/\s(?=(?:" + _CLOSING_WORDS + r")[,!.]?[ \t]*(?:\n|$))", re.I)

# Lines that can only be part of a signature block, after a closing.
_LABEL = re.compile(r"^(?i:tel|telephone|phone|mobile|mob|email|e-mail|e|web|website|fax)\.?[ \t]*:")
_LABEL_DOT = re.compile(r"^(?i:tel|mob)\.[ \t]+\S")
_URL_LINE = re.compile(r"^(?i:www\.|https?://)\S+$")
_COMPANY_SUFFIXES = {"ltd", "inc", "co", "llp", "plc", "corp", "llc"}
# Words that open a sentence or an instruction, never a name.
_NOT_A_NAME_START = {
    "i", "we", "you", "your", "our", "my", "please", "see", "call", "bring", "let",
    "note", "remember", "kindly", "hope", "looking", "speak", "talk",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
_MAX_SIGNATURE_LINES = 3
_PRESENT_WINDOW = 8


def _tidy(value) -> str:
    """CRLF/CR to LF, trailing whitespace off every line, leading/trailing blank
    lines off, runs of blank lines collapsed to one. Safe on any text: it never
    removes a word."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in (l.rstrip() for l in text.split("\n")):
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def normalise(value) -> str:
    """The stored sign-off value in its one canonical form: tidied (see _tidy) and
    only the first of two alternatives joined with ' / '. "" when nothing is
    configured. For the sign-off value only; a reply body is never split on ' / '."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _ALTERNATIVE.search(text)
    if m:
        text = text[:m.start()]
    return _tidy(text)


def ends_with(text: str, sign_off: str) -> bool:
    body, so = _tidy(text), normalise(sign_off)
    return bool(so) and (body == so or body.endswith("\n" + so))


def _in_last_lines(body: str, so: str) -> bool:
    """The configured block, as whole lines, within the last few lines of the body."""
    block, lines = so.split("\n"), body.split("\n")
    window = lines[-max(_PRESENT_WINDOW, len(block)):]
    return any(window[k:k + len(block)] == block for k in range(len(window) - len(block) + 1))


def _postscript(line: str) -> bool:
    """A P.S. / P.P.S. / N.B. line: something the writer meant to say, never a signature."""
    first = line.split(None, 1)[0] if line.split() else ""
    return re.sub(r"[^a-z]", "", first.lower()) in ("ps", "pps", "nb")


def _name_shaped(line: str) -> bool:
    """A name or a company: at most four tokens, each starting with a capital letter
    (or a bare '&'), no digits, no sentence punctuation, and not opening like a sentence."""
    s = line.strip()
    if not s or len(s) > 60 or any(c.isdigit() for c in s) or any(c in "?!;:," for c in s):
        return False
    tokens = s.split()
    if len(tokens) > 4 or tokens[0] == "&" or tokens[0].lower().strip(".") in _NOT_A_NAME_START:
        return False
    if not all(t == "&" or (t[0].isalpha() and t[0].isupper()) for t in tokens):
        return False
    return not s.endswith(".") or tokens[-1].lower().rstrip(".") in _COMPANY_SUFFIXES


def _signature_tail(tail: list[str], above: str, so_lines: set[str]) -> bool:
    """Is every line after the closing part of a signature? A line is, only when it is
    (a) a line of the configured sign-off, (b) a name or company, or (c) a labelled
    contact line (Tel:, Email:, Web:...). A web address is dropped only when nothing
    is lost: the same address is in the configured sign-off or earlier in the body."""
    if len(tail) > _MAX_SIGNATURE_LINES:
        return False
    for line in tail:
        s = line.strip()
        if _postscript(s):
            return False
        if s.lower() in so_lines:
            continue
        if _URL_LINE.match(s):
            if s.lower() in above.lower():
                continue
            return False
        if _LABEL.match(s) or _LABEL_DOT.match(s) or _name_shaped(s):
            continue
        return False
    return True


def strip_closing(text: str, sign_off: str = "") -> str:
    """Remove a closing the model wrote at the very end: a closing line ("Thanks,",
    "Kind regards,", "Thanks, Sam") plus up to three signature lines, with content
    before it. All or nothing, and only when every line after the closing is clearly a
    signature (see _signature_tail); anything else is left exactly as written."""
    lines = _tidy(text).split("\n")
    so_lines = {l.strip().lower() for l in str(sign_off or "").split("\n") if l.strip()}
    for i in range(len(lines) - 1, max(len(lines) - 2 - _MAX_SIGNATURE_LINES, -1), -1):
        if not _CLOSING_LINE.match(lines[i].strip()):
            continue
        tail = [l for l in lines[i + 1:] if l.strip()]
        above = "\n".join(lines[:i])
        if above.strip() and _signature_tail(tail, above, so_lines):
            return above.rstrip()
        break
    return "\n".join(lines).rstrip()


def apply(text: str, cfg: dict | None, channel: str = "email") -> tuple[str, str]:
    """(text with the configured sign-off exactly once, the sign-off applied).
    ("", "") for empty text so a hold stays a hold; unchanged for a non-email
    channel or when nothing is configured."""
    if channel != "email" or not (text or "").strip():
        return text or "", ""
    so = normalise((cfg or {}).get("sign_off"))
    if not so:
        return text, ""
    body = _tidy(text)
    if ends_with(body, so):
        return body, so
    stripped = strip_closing(body, so)
    if stripped != body:
        return stripped + "\n\n" + so, so
    if _in_last_lines(body, so):
        return body, so          # already there, followed by a P.S. or a link: never twice
    return body + "\n\n" + so, so
