"""Generic ignoring — mail the agent files as Automated and never answers,
by KIND rather than by sender (Kris 2026-09-13: "do we also provide generic
email ignorance, with certain email types? … add it into Inbox settings so
that it can be changed to suit").

Three rules, each a switch on tenant_config.inbox_ignore (default on):

  noreply  the sender's address says no reply is wanted — no-reply@,
           donotreply@, mailer-daemon@, postmaster@ in any spelling
  bulk     the message says it is bulk — a List-Unsubscribe header, or
           Precedence: bulk / list / junk (newsletters, marketing, lists)
  auto     the message says a machine sent it — Auto-Submitted anything
           but "no" (receipts, alerts, out-of-office, delivery reports)

Pure: a header dict + an address in, a reason out. The readers (src/app/gmail.py,
src/app/mailbox.py) carry the three headers on the email as `headers`, lowercase
keys; the pipeline and the backlog import ask here before spending a token.
"""
from __future__ import annotations

RULES: dict[str, dict] = {
    "noreply": {"label": "No-reply and bounce senders",
                "help": "no-reply@, do-not-reply@, mailer-daemon@, postmaster@ — a reply would mail a machine"},
    "bulk":    {"label": "Newsletters and bulk mail",
                "help": "mail that says so itself — an unsubscribe link in its headers, or marked bulk / list"},
    "auto":    {"label": "Auto-generated mail",
                "help": "receipts, alerts, delivery reports, out-of-office replies — marked auto-submitted"},
}
DEFAULTS = {k: True for k in RULES}
HEADERS = ("list-unsubscribe", "precedence", "auto-submitted")


def settings_of(cfg: dict | None) -> dict[str, bool]:
    """Every rule's switch, defaulted and validated — junk reads as the default."""
    stored = (cfg or {}).get("inbox_ignore") or {}
    if not isinstance(stored, dict):
        stored = {}
    out = {}
    for key, default in DEFAULTS.items():
        v = stored.get(key, default)
        out[key] = default if not isinstance(v, bool) else v
    return out


def is_no_reply_address(address: str | None) -> bool:
    """True for system 'no-reply' senders — no-reply@, noreply@, no_reply@,
    donotreply@, do-not-reply@ and the like, plus bounce/delivery-report
    senders (mailer-daemon@, postmaster@). Matches the local part with
    separators/digits stripped, so every spelling collapses to one check."""
    if not address or "@" not in address:
        return False
    local = "".join(c for c in address.split("@", 1)[0].lower() if c.isalpha())
    return ("noreply" in local or "donotreply" in local
            or "mailerdaemon" in local or local == "postmaster")


def pick_headers(pairs) -> dict[str, str]:
    """The three headers that matter, from any (name, value) iterable or a
    list of {name, value} dicts, keyed lowercase. Missing = absent."""
    out: dict[str, str] = {}
    for item in pairs or []:
        if isinstance(item, dict):
            name, value = item.get("name"), item.get("value")
        else:
            try:
                name, value = item
            except (TypeError, ValueError):
                continue
        key = str(name or "").strip().lower()
        if key in HEADERS and key not in out:
            out[key] = str(value or "").strip()
    return out


def automated_reason(email: dict | None, settings: dict[str, bool] | None = None) -> str | None:
    """Why this email is Automated (filed, never answered), or None when a
    person may be behind it. Checked in the order the rules are listed."""
    s = settings or DEFAULTS
    email = email or {}
    if s.get("noreply") and is_no_reply_address(email.get("from_address")):
        return "System no-reply sender — correctly left alone"
    hdrs = email.get("headers") or {}
    if not isinstance(hdrs, dict):
        hdrs = {}
    if s.get("bulk"):
        if (hdrs.get("list-unsubscribe") or "").strip():
            return "Bulk mail (carries an unsubscribe link) — filed, not answered"
        prec = (hdrs.get("precedence") or "").strip().lower()
        if prec in ("bulk", "list", "junk"):
            return f"Bulk mail (Precedence: {prec}) — filed, not answered"
    if s.get("auto"):
        auto = (hdrs.get("auto-submitted") or "").strip().lower()
        if auto and auto != "no":
            return f"Auto-generated mail (Auto-Submitted: {auto}) — filed, not answered"
    return None
