"""Which notifications reach you, and when.

Kris, 2026-09-02: "We get notifications from all sorts of actions on BLAM8, it
would be good to have total control over every type of notification."

Before this there was none. `push.notify()` recorded a feed row and pushed to
every subscribed device for all twenty kinds, and the only switch in the app
was the morning brief's own on/off — one kind out of twenty.

Two ideas hold the whole thing up.

**A family, not a kind.** Twenty switches is not control, it is a chore, and
nobody knows the difference between `quote_viewed` and `quote_keeps_looking`
before they have seen one. Kinds group into five things a person actually has
an opinion about, and the opinion is set per family.

**The brief is the quiet tier.** Every notification is either worth an
interruption or worth a mention in the morning — so the three settings are Now,
In the brief, and Never, and the 07:30 brief you already have becomes the
destination for everything you did not want buzzing. Quiet hours ride the same
road: an overnight event is not dropped, it is held and mentioned at breakfast.

**The feed is the record, always.** Whatever the setting, the notification is
written to concierge_notifications. "Never" means never interrupt me and never
mention it — it does not mean pretend it did not happen, because a customer
accepting a quote is a fact about the business, not a message you can decline.

That last one was a claim before it was true. Two kinds — quote_viewed and
invoice_viewed — carried their own opt-in, and it was checked at the CALL SITE
as `if cfg.get(...): push.notify(...)`, which skipped the feed row along with
the push. A tenant with the toggle off had no record their customer had ever
opened the document, on a settings page that told them in as many words that
everything is still recorded. The opt-ins live here now (OPT_INS), beside every
other question of the form "does this earn an interruption" — so the call sites
report what happened and this module decides who hears about it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time

from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

NOW, BRIEF, NEVER = "now", "brief", "never"
MODES = (NOW, BRIEF, NEVER)

# The five things a person has an opinion about, and the kinds behind each.
# Keyed by the tenant_config column that carries the setting.
FAMILIES: dict[str, dict] = {
    "notify_drafts": {
        "label": "Drafts & replies",
        "help": "A reply is drafted and waiting for you",
        "kinds": {"draft_ready"},
    },
    "notify_bookings": {
        "label": "Bookings",
        "help": "Booked, moved, or a reschedule asked for through your portal",
        "kinds": {"portal_booking_created", "portal_booking_moved",
                  "portal_reschedule_request", "dependant_expiry", "level_change",
                  "waitlist_offer", "reenrol_window", "customer_note", "certificate_expiry",
                  "hand_cancel",
                  # Someone on the job tapped On my way / Done on their job
                  # sheet (src/app/job_sheet_public.py).
                  "team_on_way", "team_job_done", "team_photos", "after_job_due",
                  # A vulnerable household on an urgent job (src/app/urgency.py, D1).
                  "vulnerable_household", "out_of_hours_ask", "alert_words"},
    },
    "notify_questions": {
        "label": "Customer questions",
        "help": "Somebody asks a question through your portal",
        "kinds": {"portal_escalation", "owner_rule"},
    },
    "notify_quotes": {
        "label": "Quotes",
        "help": "Drafted, opened, accepted, declined, questioned or expired",
        "kinds": {"quote_drafted", "quote_viewed", "quote_accepted",
                  "quote_declined", "quote_question", "quote_expired",
                  "quote_keeps_looking"},
    },
    "notify_invoices": {
        "label": "Invoices & payments",
        "help": "Paid, opened, overdue, refunded, disputed, or a payment that "
                "didn't go through",
        "kinds": {"invoice_paid", "invoice_viewed", "invoice_overdue",
                  "invoice_payment_seen", "invoice_payment_failed", "package_balance",
                  # Card payments (docs/plan-payments-panel.md): a refund from
                  # BLAM8 or from Stripe's own dashboard, a dispute opened, and a
                  # bank payment submitted but not yet cleared.
                  "invoice_refunded", "invoice_disputed", "invoice_payment_pending",
                  # A balance invoice drafted for you (src/app/jobs/raise_balances.py).
                  "invoice_balance_ready"},
    },
    # The sixth family is the odd one out, deliberately (Kris 2026-09-04).
    # Every other family is about the world — a customer did something. This
    # one is about the product: your mailbox has stopped being readable, your
    # calendar has stopped syncing, you have used up your emails. It is the
    # only family with NO "never", because an operator who switches it off is
    # buying a week of silent downtime they will discover from a customer.
    # "In the brief" stays available: overnight, tomorrow morning is soon
    # enough, and that is a real preference rather than an off switch.
    "notify_problems": {
        "label": "Problems",
        "help": "Your mailbox needs reconnecting, calendar sync stops, or "
                "you've used up your emails",
        "kinds": {"mailbox_disconnected", "mailbox_reconnected",
                  "calendar_sync_failing", "email_cap_reached"},
        "no_never": True,
    },
}

# Families that refuse NEVER. Kept as data rather than a hardcoded column name
# so mode() and the settings form read the same source.
_NO_NEVER = {col for col, f in FAMILIES.items() if f.get("no_never")}

# Two kinds inside their families carry their own opt-in, because "they opened
# it" is a much softer signal than "they accepted it" and some people want one
# without the other. kind -> (column, default when unset).
#
# These used to be checked at the CALL SITES (Kris 2026-09-04), which wrapped
# the whole push.notify in `if cfg.get(...)`. That skipped the feed row too, so
# a tenant with the toggle off had no record that the customer ever opened the
# document — the one break left in "the feed is the record, always", and since
# yesterday it also meant the event never reached Today's deck. The decision
# lives here now, beside every other "does this earn an interruption".
OPT_INS: dict[str, tuple[str, bool]] = {
    # On by default: a customer opening a quote is how you know to follow up.
    "quote_viewed": ("notify_quote_viewed", True),
    # Off by default: an invoice being opened rarely needs you to do anything.
    "invoice_viewed": ("notify_invoice_viewed", False),
}


def opted_out(cfg: dict | None, kind: str) -> bool:
    """Has this tenant turned off this particular signal? Only the two `viewed`
    kinds can be — everything else answers to its family alone."""
    entry = OPT_INS.get(kind)
    if entry is None:
        return False
    col, default = entry
    value = (cfg or {}).get(col)
    return not (default if value is None else bool(value))

# Kinds that answer to nobody's family setting:
#   morning_debrief — it IS the brief, and has its own time/days settings;
#                     routing it through the brief would be circular.
#   support_ticket / operator_signup — platform infrastructure on the operator's own
#                     tenant, already kept out of that tenant's feed.
#   concierge_tasks_done — the answer to a task list the owner just gave; not
#                     news about a customer, and holding it would hide the reply.
UNGOVERNED = {"morning_debrief", "support_ticket", "operator_signup", "concierge_tasks_done"}

_BY_KIND = {k: col for col, f in FAMILIES.items() for k in f["kinds"]}


def family_of(kind: str) -> str | None:
    """The settings column governing `kind`, or None if nothing governs it."""
    return _BY_KIND.get(kind)


def modes_for(col: str) -> tuple[str, ...]:
    """Which settings this family actually offers. Problems offer no NEVER."""
    return (NOW, BRIEF) if col in _NO_NEVER else MODES


def mode(cfg: dict | None, kind: str) -> str:
    """Now / brief / never for this kind. Unknown kinds deliver — a notification
    nobody has classified yet must not go silently missing; it shows up, gets
    noticed, and gets a family.

    A stored value the family does not offer resolves to NOW, so a hand-made
    POST cannot switch off a family the form refuses to switch off. The check
    is here rather than only in the handler because this is what delivery
    actually reads."""
    col = family_of(kind)
    if col is None:
        return NOW
    value = (cfg or {}).get(col)
    return value if value in modes_for(col) else NOW


def _parse(value) -> time | None:
    """HH:MM (or a bare hour) into a time. Anything unreadable means unset —
    quiet hours that fail open never silence anything by accident."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parts = text.split(":")
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return None


def in_quiet_hours(cfg: dict | None, at: datetime | None = None) -> bool:
    """Is `at` inside the tenant's quiet hours? Wraps midnight, which is the
    normal case — 22:00 to 07:00 is one window, not two."""
    start = _parse((cfg or {}).get("quiet_start"))
    end = _parse((cfg or {}).get("quiet_end"))
    if not start or not end or start == end:
        return False
    now = (at or datetime.now(DIARY_TZ)).timetz()
    current = time(now.hour, now.minute)
    if start < end:
        return start <= current < end
    return current >= start or current < end        # crosses midnight


def should_push(cfg: dict | None, kind: str, at: datetime | None = None) -> bool:
    """Does this one earn an interruption right now?"""
    if kind in UNGOVERNED:
        return True
    if opted_out(cfg, kind) or mode(cfg, kind) != NOW:
        return False
    return not in_quiet_hours(cfg, at)


def belongs_in_brief(cfg: dict | None, kind: str,
                     created_at: datetime | None = None) -> bool:
    """Did this one get held for the morning?

    Two ways in: the family is set to `brief`, or the family is set to `now` but
    it happened during quiet hours, so the push was withheld. `never` is never
    mentioned, and the ungoverned kinds are never queued — the brief is one of
    them and cannot contain itself.
    """
    if kind in UNGOVERNED or opted_out(cfg, kind):
        return False           # turned this one off = don't mention it either
    m = mode(cfg, kind)
    if m == BRIEF:
        return True
    return m == NOW and in_quiet_hours(cfg, created_at)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def email_target(cfg: dict | None, tenant_mailbox: str | None) -> str | None:
    """Where notification mail should go, or None when the channel is off.

    Refuses the address BLAM8 is MONITORING. The mail is sent from the tenant's
    own connected mailbox — there is no transactional sender in the product —
    so addressing it to that same inbox would drop a notification into the pile
    the agent reads and invite it to draft a reply to itself. Nothing about the
    rest of the app stops that, so it is stopped here, at the only place that
    knows both addresses.
    """
    address = ((cfg or {}).get("notify_email") or "").strip().lower()
    if not address or not EMAIL_RE.match(address):
        return None
    if tenant_mailbox and address == tenant_mailbox.strip().lower():
        return None
    return address


def quiet_window(cfg: dict | None) -> str | None:
    """"22:00–07:00", or None when quiet hours are not set."""
    start, end = _parse((cfg or {}).get("quiet_start")), _parse((cfg or {}).get("quiet_end"))
    if not start or not end or start == end:
        return None
    return "%02d:%02d–%02d:%02d" % (start.hour, start.minute, end.hour, end.minute)


def summary(cfg: dict | None) -> str:
    """One line for the settings face: what is actually reaching you."""
    counts = {NOW: 0, BRIEF: 0, NEVER: 0}
    for col in FAMILIES:
        counts[mode(cfg, next(iter(FAMILIES[col]["kinds"])))] += 1

    if counts[NOW] == len(FAMILIES):
        head = "Everything, as it happens"
    elif counts[NEVER] == len(FAMILIES):
        head = "Nothing"
    else:
        head = " · ".join(
            bit for bit, n in (("%d now" % counts[NOW], counts[NOW]),
                               ("%d in the brief" % counts[BRIEF], counts[BRIEF]),
                               ("%d off" % counts[NEVER], counts[NEVER])) if n)
    parts = [head]
    window = quiet_window(cfg)
    if window:
        parts.append("quiet " + window)
    # The brief is half of what this section does, and it is the half with a
    # time on it — a face that omits it would leave you opening the row to
    # answer "when does the brief come?".
    c = cfg or {}
    if c.get("debrief_enabled") is None or c.get("debrief_enabled"):
        parts.append("brief " + str(c.get("debrief_time") or "07:30"))
    else:
        parts.append("no brief")
    return " · ".join(parts)
