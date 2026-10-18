"""A vulnerable household on an urgent job is flagged on Today
(docs/plan-service-urgency.md, D1 stage 1).

The research behind it (reports/Plumber emergency and out of hours.md): small
firms move a job up when the household has small children, elderly or ill
people, and the DMCC Act 2024 s.247 judges fairness against someone made
vulnerable by their circumstances. Nothing here re-orders anyone's day — the
owner is told, and decides. It only speaks for a business that has marked a
service urgent, and only when the email names BOTH who is at risk and what
has gone wrong.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

log = logging.getLogger(__name__)

KIND = "vulnerable_household"

_WHO = re.compile(
    r"\b(?:bab(?:y|ies)|newborn|toddlers?|infants?|(?:small|young|little) (?:kids|children|ones)"
    r"|kids|children|elderly|pensioners?|(?:my|our) (?:mum|mom|dad|nan|nana|gran|granny|grandad|grandma|grandpa)"
    r"(?:'s| is)? (?:\d{2}|in (?:her|his) (?:80s|90s))|\b[7-9]\d[- ]year[- ]old"
    # …and said the way people write it (live 2026-09-30: "I've got my mum staying with
    # us, she's 84"): an older relative, then "she's / he's / they're" and an age of 70+
    # later in the same sentence.
    r"|(?:mum|mom|mother|dad|father|nan|nana|gran|granny|grandad|grandma|grandpa)\b[^.!?\n]{0,60}?"
    r"\b(?:she|he|they)(?:'s| is| are|'re) (?:[7-9]\d|1[01]\d)"
    r"|disabled|disability|wheelchair|carer|dementia|oxygen|dialysis|chemo(?:therapy)?"
    r"|pregnant|heart condition|medical condition|poorly|unwell|vulnerable)\b",
    re.IGNORECASE)
# WHAT: that something is urgent — in words any customer of any business uses
# (agnostic by rule, Kris 2026-09-26: the first version needed an appliance
# noun or appliance-specific failure phrase, so it could never fire for other
# business types). The business's
# own urgent SERVICE already says what counts as an emergency for them.
_WHAT = re.compile(
    r"\b(?:urgent(?:ly)?|emergency|asap|as soon as (?:possible|you can)|straight away"
    r"|right away|immediately|today|tonight|same[- ]day|can't wait|cannot wait"
    r"|no [a-z]+(?: [a-z]+)? (?:since|at all|for (?:days|hours|two|three|\d))"
    r"|(?:stopped|not) working|broken down|burst|leak(?:ing)?|flood(?:ing|ed)?)\b",
    re.IGNORECASE)


def flag(text: str | None) -> tuple[str, str] | None:
    """(who, what) as the email said them, when it names both — else None."""
    t = text or ""
    who, what = _WHO.search(t), _WHAT.search(t)
    return (who.group(0), what.group(0)) if who and what else None


def maybe_flag(tenant_id: str, *, contact: str | None, name: str | None,
               body: str | None, service_types=None) -> bool:
    """Raise the Today flag when the business has an urgent service and the
    email names a vulnerable household with an urgent problem. Best-effort."""
    from app import push, services
    try:
        if not any(services.is_urgent(s) for s in services.clean_service_types(service_types)):
            return False
        hit = flag(body)
        if not hit or not contact:
            return False
        who, what = hit
        push.notify(tenant_id,
                    title=f"{name or contact}: urgent, and {who.lower()}",
                    body="Their reply is drafted — worth a look at today's order in case they "
                         "should come first.",
                    url="/app/conversations/view?contact=" + quote(contact),
                    tag=f"{KIND}-{contact}", kind=KIND, contact=contact, contact_name=name)
        return True
    except Exception:
        log.warning("vulnerable-household flag failed (tenant=%s)", tenant_id, exc_info=True)
        return False


OOH_KIND = "out_of_hours_ask"


def ask_owner_ooh(tenant_id: str, *, contact: str | None, name: str | None,
                  withheld: list[dict]) -> bool:
    """Out of hours, "ask me first" (D1 stage 2): the reply held the standard
    slot and said nothing of the evening; Today asks the owner. Their yes is a
    redraft in their words ("offer tonight") — the executor then may offer it."""
    from app import push
    if not withheld or not contact:
        return False
    first = withheld[0]
    slot = (first.get("slots") or [{}])[0]
    from app import scheduling
    try:
        when = scheduling.arrival_words(slot.get("date"), slot.get("window")) or "out of hours"
    except Exception:
        when = "out of hours"
    push.notify(tenant_id,
                title=f"{name or contact}: {first['service'].lower()} out of hours? "
                      f"£{first['price']:g} — {when}".strip(),
                # The Today card shows the body, not the title (rehearsal
                # 2026-09-26: it read "Out of hours?" with no who, when or
                # price) — so the body carries them, and the owner's next step.
                body=(f"{name or contact} could have {first['service'].lower()} {when} at "
                      f"£{first['price']:g} (out of hours). Their reply offers the next "
                      f"standard time — to offer this instead, open the draft, tap 👎 and "
                      f"say “offer {when}”."),
                url="/app/conversations/view?contact=" + quote(contact),
                tag=f"{OOH_KIND}-{contact}", kind=OOH_KIND, contact=contact, contact_name=name)
    return True


ALERT_KIND = "alert_words"


def alert_phrases(raw: str | None) -> list[str]:
    """The owner's "Always tell me when an email mentions" list (Settings ›
    Rules & escalation), comma- or line-separated, lower-cased, deduped."""
    out: list[str] = []
    for part in re.split(r"[,\n]", raw or ""):
        p = " ".join(part.split()).lower()[:60]
        if len(p) >= 2 and p not in out:
            out.append(p)
    return out[:40]


def alert_match(body: str | None, phrases: list[str]) -> str | None:
    """The first of the owner's phrases the email says, as a whole phrase."""
    text = body or ""
    for p in phrases:
        if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text, re.IGNORECASE):
            return p
    return None


def alert_flag(tenant_id: str, *, contact: str | None, name: str | None, body: str | None,
               phrases: str | None) -> bool:
    """One of the OWNER's own alert words reached Today, whether the reply
    waits or went out on its own (full-auto test 2026-09-26: a services rig's
    danger report got the right fast reply and nobody at the business knew).
    Agnostic by rule: each business lists its own urgent phrases in its
    settings, so nothing here knows any trade."""
    from app import push
    hit = alert_match(body, alert_phrases(phrases)) if contact else None
    if not hit:
        return False
    push.notify(tenant_id, title=f"{name or contact}: \u201c{hit}\u201d mentioned",
                body="One of your alert words is in their email — check the thread and the reply.",
                url="/app/conversations/view?contact=" + quote(contact),
                tag=f"{ALERT_KIND}-{contact}", kind=ALERT_KIND, contact=contact, contact_name=name)
    return True


RULE_KIND = "owner_rule"


def rule_note(tenant_id: str, *, contact: str | None, name: str | None, rule: str,
              what: str = "") -> bool:
    """One of the owner's rules shaped a reply that went out on its own — a
    quiet Today note so they know it came up (full-auto test 2026-09-26: a
    price match was refused per "never price match", and nobody knew)."""
    from app import push
    if not contact or not (rule or "").strip():
        return False
    push.notify(tenant_id, title=f"{name or contact}: your rule applied",
                body=(f"{what.strip().rstrip('.')} — " if what.strip() else "") + f"per “{rule.strip()}”.",
                url="/app/conversations/view?contact=" + quote(contact),
                tag=f"{RULE_KIND}-{contact}", kind=RULE_KIND, contact=contact, contact_name=name)
    return True
