"""Business Q&A tool layer — the concierge's READ-ONLY window onto a tenant's
own data (docs/modules/module-concierge.md, business mode / Phase 1).

Design (the house retrieval rule): the model NEVER writes a query. It picks from
this fixed catalogue of read-only tools; every executor here takes tenant_id from
the server session plus the caller's RLS-bound client — belt (explicit tenant_id
filter in every db helper) and braces (RLS on every table). There is no write
path anywhere in this module.

Tier gating: the CRM/diary/follow-up tools are the Pro unlock (the 'crm' module
— the same boundary as the CRM pages themselves). On Starter the executor
returns a `locked` payload instead of data, and the engine's prompt tells the
model to relay the upgrade route honestly.

Caps: results per tool call are capped at MAX_ROWS (context overflow guard);
the engine caps calls per question (runaway-loop guard). Bodies are snipped —
tools return evidence snippets, never whole mail archives.
"""
import logging
import re
from datetime import datetime, timezone

from app import db, slotgrid
from app import concierge_reads, inbox_labels
from app import knowledge as kb

log = logging.getLogger(__name__)

MAX_ROWS = 50          # hard ceiling per tool call, whatever the model asks for
SNIPPET_CHARS = 400    # email-body / KB-chunk excerpt length

# The Pro unlock: these tools exist only when the 'crm' module is visible —
# exactly the boundary the CRM pages gate on (db.visible_modules).
# What a tool says when the asker's door is shut. Phrased so the model relays
# a fact about permissions rather than inventing an outage.
CLOSED_DOOR = {"error": "not_your_door",
               "message": "That part of the account isn't open to you. "
                          "Ask whoever runs it if you need it."}

# What a CRM tool says when the customer asked about is not one of theirs.
# Deliberately different words from CLOSED_DOOR: the door IS open, this is
# somebody else's customer behind it.
NOT_THEIRS = {"error": "not_your_customer",
              "message": "That customer isn't one of the ones you work with. "
                         "Ask whoever runs the account if you need them."}

PRO_TOOLS = frozenset((
    "search_contacts", "get_contact_timeline", "list_deals",
    "get_pipeline_summary", "list_followups", "list_bookings",
    "get_contact_record", "list_dormant_contacts",
))

LOCKED = {"locked": True,
          "message": "CRM, pipeline, follow-up and diary queries are part of the "
                     "Pro plan. The customer can upgrade on the Plans & billing "
                     "page — answer inbox and knowledge questions as normal."}

# ── The catalogue (Anthropic tool definitions) ───────────────────────────────
TOOLS = [
    {
        "name": "get_inbox_overview",
        "description": (
            "The current state of the ENTIRE inbox, exactly as the owner's "
            "conversations page shows it — THE one call for 'what needs replying "
            "to?', 'what needs my attention?', 'what's outstanding?', 'anything "
            "urgent?', 'am I caught up?' and every other INBOX-status question. "
            "It does NOT know about the Confirm/Cancel cards the owner has not "
            "answered — that is list_awaiting_approval. Takes no filters on "
            "purpose: 'today' in those questions means what's on the owner's "
            "plate NOW, so an unanswered email from weeks ago counts (it's MORE "
            "urgent, not less). Returns `needs_attention` — every conversation "
            "whose label is needs_reply (waiting for a response, oldest first), "
            "draft_ready (a draft awaits the owner's approval) or "
            "booking_pending (a booking is held or confirmed) — plus `counts` "
            "for all six labels and `suppressed_contacts` (senders the agent "
            "PAUSED drafting for because the owner kept not replying; mention "
            "them — the owner may be ignoring them accidentally, and can "
            "resume, ignore permanently, or draft a one-off from the thread). "
            "Only say the owner is caught up when needs_attention comes back "
            "empty from THIS call."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_conversations",
        "description": (
            "Search the business's email conversations (the agent's inbox) for "
            "specific topics, people or periods — for the overall 'what needs my "
            "attention?' question use get_inbox_overview instead. Use `query` "
            "for topic words (searches subject + body), `contact` for a "
            "person/company name or address fragment, ISO dates to bound when the "
            "email arrived, and `status` to narrow: 'pending' = a draft is waiting "
            "for approval (needs a reply), 'sent' = answered, 'skipped' = no reply "
            "was drafted. `category` filters by the inbox's smart label: "
            "'needs_reply' = waiting for a response, 'draft_ready' = a draft awaits "
            "approval, 'quote_sent' = replied and awaiting the customer, "
            "'booking_pending' = a booking is held or confirmed, 'fyi'/'automated' "
            "= no action needed. Never combine `category` with date bounds for "
            "status questions — an old email still needing a reply counts. "
            "Returns snippets — follow up with get_conversation for a "
            "full thread. The result may also carry `suppressed_contacts`: senders "
            "the owner kept not replying to, so the agent PAUSED drafting for them "
            "(their mail still arrives under FYI). When asked what needs replying "
            "to / what's outstanding, mention these too."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "topic words to search subject + body for"},
                "contact": {"type": "string", "description": "sender name, company or email fragment"},
                "date_from": {"type": "string", "description": "ISO date lower bound (inclusive)"},
                "date_to": {"type": "string", "description": "ISO date upper bound (inclusive)"},
                "status": {"type": "string", "enum": ["", "pending", "approved", "sent", "skipped"]},
                "category": {"type": "string",
                             "enum": ["", "needs_reply", "draft_ready", "quote_sent",
                                      "booking_pending", "fyi", "automated"]},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_conversation",
        "description": (
            "Fetch one full conversation thread by the contact's email address "
            "(as returned by search_conversations) — every inbound message and "
            "the agent's reply/draft for each, oldest first."),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "the contact's email address"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "search_contacts",
        "description": (
            "Search the CRM contacts by name, company or email fragment — or "
            "by a dependant's name (a pet, vehicle, child…), which returns its "
            "owner with `matched_dependant`. Empty query lists the most "
            "recently active contacts. Returns linked company and "
            "last-activity date."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_contact_timeline",
        "description": (
            "One customer's chronological activity timeline from the CRM — "
            "emails and SMS both ways, diary bookings, invoices "
            "sent/paid/chased, quotes, and everything they did on the "
            "customer portal (questions they asked the assistant, self-serve "
            "bookings, reschedules and reschedule requests). THE call for "
            "'what has X been up to?', 'what did X ask on the portal?', "
            "'any recent activity from X?'. Pass the contact's email address "
            "— use search_contacts first when you only have a name."),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "the contact's email address"},
                "limit": {"type": "integer"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "list_deals",
        "description": (
            "List CRM deals (the pipeline's rows). Filter by `stage` (a stage "
            "name from get_pipeline_summary, e.g. 'Prospect'), `status` "
            "('open'|'won'|'lost'|'dormant', default open), or `stale_after_days` "
            "(only deals with no activity for at least N days — use for 'who "
            "haven't I followed up with?')."),
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "status": {"type": "string", "enum": ["", "open", "won", "lost", "dormant"]},
                "stale_after_days": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_pipeline_summary",
        "description": ("Counts and total value of open deals per pipeline stage — "
                        "the one-call answer to 'how many prospects / what's my "
                        "pipeline look like?'."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_followups",
        "description": (
            "Open follow-ups: promises made in email (both directions — what WE "
            "owe them and what THEY owe us) and open CRM tasks with due dates. "
            "Use for 'what do I owe people?' / 'what's outstanding?'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "due_before": {"type": "string", "description": "ISO date — only tasks due on/before this"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_bookings",
        "description": (
            "Diary bookings. Filter by ISO date range, contact email fragment, "
            "and `status` — default is the live diary (proposed + confirmed); "
            "pass 'cancelled' or 'completed' to see those instead. A multi-night "
            "STAY is in on every day it covers: one that began before date_from "
            "and is still running comes back marked started_before_range, and "
            "every stay carries arrives / leaves / nights — so 'who's in on the "
            "13th' is every stay whose arrives ≤ that day ≤ leaves. `for` names "
            "the pets/dependants linked to the booking (else they're in `what`). "
            "`work` is the kind of work (e.g. 'Drop-offs and collections', 'Meet & "
            "greets') and `on_it` names the people on the job. `person` (a team "
            "member's name — or the asker's own, for 'my jobs') returns only that "
            "person's work: jobs they're on, plus the kinds of work they do."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "contact": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["", "proposed", "confirmed", "declined", "cancelled", "completed"]},
                "person": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the business's own knowledge base — their prices, policies, "
            "services, opening hours. The source for 'what do we charge for X?' "
            "style questions."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_invoices",
        "description": (
            "The business's invoices — ref, customer, total, status, due date, "
            "days overdue, reminders already sent, and the invoice_id every "
            "invoice action needs. Filter by status ('draft', 'sent', 'overdue', "
            "'paid') — 'overdue' also catches sent invoices past their due date. "
            "Use for 'which invoices are overdue?', 'has X paid?', and before "
            "proposing invoice_send / invoice_mark_paid / invoice_reminder_draft / "
            "invoice_edit_draft / invoice_delete_draft. A draft's lines (description and "
            "amount) are included, so a change to one can be written as the full new set; "
            "a sent invoice's are not."),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["", "draft", "sent", "overdue", "paid",
                                    "partially_paid", "cancelled"]},
                "contact": {"type": "string",
                            "description": "customer email or name fragment"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_quotes",
        "description": (
            "The business's quotes — ref, customer, total, status, validity, "
            "whether (and how often) the customer has viewed it, chases sent, "
            "and the quote_id every quote action needs. Filter by status "
            "('draft', 'sent', 'accepted', 'declined', 'expired', 'invoiced') — "
            "'expired' also catches sent quotes past their valid-until date. "
            "Use for 'which quotes are still out?', 'did they open my quote?', "
            "'what have I quoted this month?', and before proposing any "
            "quote_* action (quote_edit_draft / quote_delete_draft included). A draft's "
            "lines (description and amount, optional ones marked) are included, so a "
            "change to one can be written as the full new set; a sent quote's are not."),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["", "draft", "sent", "accepted", "declined",
                                    "expired", "invoiced", "cancelled"]},
                "contact": {"type": "string",
                            "description": "customer email or name fragment"},
                "limit": {"type": "integer"},
            },
        },
    },
]

TOOLS += [
    {
        "name": "get_availability",
        "description": (
            "Whether a visit FITS the diary on given days — the scheduling engine's "
            "answer (capacity at once, the gap between jobs, working days, blackout "
            "dates, the customer notice rule), NOT a count of booking rows. ALWAYS use "
            "this for 'is X free / can we fit / when could we do' questions; "
            "list_bookings alone cannot answer them. Pass the service name when the "
            "owner names one (length differs); `window` narrows to morning/afternoon. "
            "If the result carries availability_unknown, say you can't check "
            "availability from here — never 'full' or 'booked'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO date"},
                "date_to": {"type": "string", "description": "ISO date (same day if omitted; max 14 days)"},
                "service": {"type": "string", "description": "a service from the menu, optional"},
                "window": {"type": "string", "description": "morning / afternoon / day / HH:MM, optional"},
            },
            "required": ["date_from"],
        },
    },
    {
        "name": "get_contact_record",
        "description": (
            "Everything on ONE customer's record: their custom fields (regular "
            "cleaner, access arrangements, alarm/key-safe codes, vehicle reg, level…), "
            "dependants (pets, cars, children, properties) with their facts and "
            "expiring dates, documents on file, open promises both ways, the customer "
            "log, credits/packages, and their next and last booking. Use this FIRST "
            "for 'what do I know about / what's X's … / is Y's Z in date' questions. "
            "Values flagged never_in_customer_email are still the owner's to see here."),
        "input_schema": {
            "type": "object",
            "properties": {"contact": {"type": "string",
                                       "description": "the customer's email, or their name"}},
            "required": ["contact"],
        },
    },
    {
        "name": "list_dormant_contacts",
        "description": (
            "Customers with NO booking on or after a date (or none ever): who has "
            "lapsed, who hasn't rebooked, who only enquired. Oldest first, with each "
            "one's last booking and last activity. Use for 'who haven't I seen since / "
            "who's gone quiet / who hasn't rebooked'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO date; default 90 days ago"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_expiring",
        "description": (
            "Every EXPIRING date across all customers — a pet's vaccinations, a "
            "car's MOT, a customer's own certificate — that has run out or runs "
            "out within `within_days` (default 30), soonest first, with the owner, "
            "their next booking, and blocks_next_booking (it runs out before that "
            "booking ends, so the booking will be held back). Use for 'whose "
            "vaccinations run out this month / what's expiring / who needs chasing "
            "for an up-to-date card'. `detail` narrows to one kind of date "
            "('Vaccinations')."),
        "input_schema": {
            "type": "object",
            "properties": {
                "within_days": {"type": "integer", "description": "look this many days ahead; default 30"},
                "detail": {"type": "string", "description": "optional: only this date, e.g. 'Vaccinations'"},
                "limit": {"type": "integer"},
            },
        },
    },
    # Keep this historical position: the agnostic render baselines key debt by
    # tool position (tests/fixtures/agnostic/debt.json). Append new tools below
    # this list so existing entries do not shift.
    {
        "name": "list_awaiting_approval",
        "description": (
            "THIS person's own work in hand and what became of it, read from the records — "
            "THE call for 'what's awaiting my approval?', 'anything to confirm?', 'did I leave "
            "something unconfirmed?', the bare 'what's waiting for me?', and also 'what did you "
            "do?', 'what have I signed off?', 'what happened to that one?' (or 'to the text to Pat?'), "
            "'did that go?', 'where are we with X?': results land after a turn ends, so call this for "
            "any status or outcome, not the chat; what was only said or asked needs no call. "
            "`status`: 'open' (default) = cards waiting for their tap, expired or parked ones, tasks "
            "being worked on, drafts awaiting approval; 'recent' = what went through, was answered or "
            "was not done, prepared in the last `since_days` (default 7, at most 30); 'all'. "
            "'recent' and 'all' also show what became of each part of a message that asked for "
            "several things at once. `customer` narrows to one name. Each item has a plain state, and ONLY 'Went through' "
            "means a change happened: nothing in any other state has happened. Shows only their own "
            "items. Emails the inbox agent drafted are get_inbox_overview's Draft Ready group. "
            "Changes nothing."),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "recent", "all"]},
                "since_days": {"type": "integer"},
                "customer": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
]

from app.platform_help import TOOL as _PRODUCT_HELP_TOOL
TOOLS += [_PRODUCT_HELP_TOOL]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

# Which door each tool reads through (docs/plan-people.md slice 4). A staff
# member whose Money door is shut must not be able to ask the assistant who
# owes the business money — the pages hid it, and an assistant that answers
# anyway is a hole in the same wall. None = no door: the business's own
# policies and prices are for everyone who works here.
TOOL_DOORS: dict[str, str | None] = {
    "get_inbox_overview": "inbox",
    "search_conversations": "inbox",
    "get_conversation": "inbox",
    "search_contacts": "crm",
    "get_contact_timeline": "crm",
    "get_contact_record": "crm",
    "list_dormant_contacts": "crm",
    "list_expiring": "crm",
    "list_deals": "crm",
    "get_pipeline_summary": "crm",
    "list_followups": "crm",
    "list_bookings": "diary",
    "get_availability": "diary",
    "list_invoices": "money",
    "list_quotes": "money",
    "search_knowledge_base": None,
    # Their own cards, already door-checked when they were staged.
    "list_awaiting_approval": None,
    "get_product_help": None,
}


# CRM tools that are the WHOLE BOOK rather than one customer. A person whose
# CRM is the customers behind their own work has no version of these — the
# pages don't offer them either (dashboard._CRM_WHOLE_BOOK), and an assistant
# that answered "who has gone quiet?" across every customer would be a way
# round the page that refuses it.
WHOLE_BOOK_TOOLS = frozenset((
    "list_dormant_contacts", "list_deals", "get_pipeline_summary", "list_followups",
    "list_expiring",
))


def tool_door(name: str) -> str | None:
    """The door a tool reads through. An unmapped tool is treated as the
    Money door — the most cautious one — so a tool added later is closed to
    staff until somebody decides where it belongs, rather than open by
    default."""
    return TOOL_DOORS.get(name, "money") if name in TOOL_NAMES else "money"


def tools_for(full_qa: bool, member: dict | None = None) -> list[dict]:
    """The catalogue this tenant's tier may call, narrowed to the doors THIS
    person may open. Starter still SEES no Pro tools at all (cleaner than a
    refusal loop); the engine prompt explains the upgrade. `member` None is an
    account that predates People — its single user owns it, so nothing is
    narrowed."""
    out = TOOLS if full_qa else [t for t in TOOLS if t["name"] not in PRO_TOOLS]
    if member is None:
        return out
    from app import crm_visibility, people
    whole_book_shut = crm_visibility.narrows_contacts(member)
    return [t for t in out
            if (tool_door(t["name"]) is None
                or people.can_open(member, tool_door(t["name"])))
            and not (whole_book_shut and t["name"] in WHOLE_BOOK_TOOLS)]


# ── Executors ────────────────────────────────────────────────────────────────
def _s(args: dict, key: str, max_len: int = 200) -> str | None:
    val = str((args or {}).get(key) or "").strip()[:max_len]
    return val or None


def _iso_date(args: dict, key: str) -> str | None:
    """A YYYY-MM-DD the model supplied, or None — anything malformed is dropped
    rather than passed into a filter."""
    val = _s(args, key, 10)
    if not val:
        return None
    try:
        datetime.strptime(val, "%Y-%m-%d")
        return val
    except ValueError:
        return None


def _limit(args: dict) -> int:
    try:
        return max(1, min(int((args or {}).get("limit") or MAX_ROWS), MAX_ROWS))
    except (TypeError, ValueError):
        return MAX_ROWS


def _snip(text: str | None, n: int = SNIPPET_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def _suppressed_summary(tenant_id, client, member=None) -> list[dict]:
    """Draft-suppression sidecar for search_conversations: the senders the agent
    stopped drafting to because the owner never answered (they'd otherwise be
    invisible in a 'what needs replying?' sweep — their rows all sit under FYI).
    Best-effort: no stats, no sidecar, same answer as before the feature.

    A person whose Inbox is their own mailbox sees only the paused senders
    whose mail reached one of THEIR mailboxes (security review 2026-09, M35)."""
    from app import people
    try:
        rows = db.list_draft_suppressed(tenant_id, client=client)[:10]
        if people.inbox_is_theirs_only(member):
            rows = [s for s in rows if s.get("address") and _their_mailbox_only(
                tenant_id, client, member,
                db.get_conversation(tenant_id, s.get("address"), client=client))]
        return [{
            "contact_email": s.get("address"),
            "status": ("ignored permanently" if s.get("status") == "permanently_ignored"
                       else "drafting paused"),
            "unanswered_drafts": s.get("consecutive_unsent_drafts"),
            "emails_since_paused": s.get("emails_since_suppressed"),
            "since": (s.get("suppressed_at") or "")[:10] or None,
        } for s in rows]
    except Exception:
        return []


def _convo_row(c: dict) -> dict:
    """One snapshot conversation in the tool-result shape — the same vocabulary
    the row-level search returns, so the model never sees two schemas."""
    return {
        "contact_email": c.get("contact"),
        "contact_name": c.get("display_name") or c.get("from_name") or c.get("contact_name"),
        "company": c.get("contact_company"),
        "subject": c.get("subject"),
        "snippet": _snip(c.get("preview"), 200),
        "status": c.get("state"),
        "category": c.get("category"),
        "has_draft_waiting": c.get("state") in ("pending", "approved"),
        "summary": c.get("request_summary"),
        "date": c.get("last_at"),
    }


async def _get_inbox_overview(tenant_id, client, args, member=None) -> dict:
    """The whole inbox as the conversations page computes it (shared snapshot,
    src/app/inbox_labels.py) — counts per smart label plus every conversation the
    owner has something to do about. No filters by design: 'what needs replying
    to today?' means what's on their plate now, not what arrived today."""
    convos = _their_mailbox_only(
        tenant_id, client, member,
        await inbox_labels.conversation_snapshot(tenant_id, client))
    counts = {k: 0 for k in inbox_labels.CONVO_CATEGORIES}
    for c in convos:
        counts[c["category"]] += 1
    active = []
    for cat in inbox_labels.ACTIVE_CATEGORIES:
        rows = [c for c in convos if c["category"] == cat]
        if cat == "needs_reply":
            rows.sort(key=lambda c: c.get("last_at") or "")  # waited longest = most urgent
        active += rows[:MAX_ROWS]
    suppressed = _suppressed_summary(tenant_id, client, member)
    extra = {"suppressed_contacts": suppressed} if suppressed else {}
    total = sum(counts[k] for k in inbox_labels.ACTIVE_CATEGORIES)
    # "draft_ready" counts every draft awaiting a yes, whatever the thread's
    # label — the same number as the Inbox's Draft Ready pill (2026-09-26).
    counts["draft_ready"] = sum(1 for c in convos if c.get("state") == "pending")
    return {**extra,
            "needs_attention": [_convo_row(c) for c in active],
            "counts": counts,
            "total_needing_attention": total}


async def _search_conversations(tenant_id, client, args, member=None) -> dict:
    category = _s(args, "category", 20)
    if category in inbox_labels.CONVO_CATEGORIES:
        # The smart label is DERIVED (legacy rows store NULL, bookings adjust at
        # read time, brand-new senders have no email_log row at all), so a
        # category filter must run over the same snapshot the inbox page
        # renders — a raw email_log.category match returned 0 for an inbox
        # showing 6 and the concierge said "all clear" (live 2026-07-26).
        convos = [c for c in _their_mailbox_only(
                      tenant_id, client, member,
                      await inbox_labels.conversation_snapshot(tenant_id, client))
                  if c["category"] == category]
        contact = (_s(args, "contact") or "").lower()
        if contact:
            convos = [c for c in convos if contact in " ".join(filter(None, (
                c.get("contact"), c.get("display_name"), c.get("from_name"),
                c.get("contact_name"), c.get("contact_company")))).lower()]
        q = (_s(args, "query") or "").lower()
        if q:
            convos = [c for c in convos if q in " ".join(filter(None, (
                c.get("subject"), c.get("preview")))).lower()]
        status = _s(args, "status", 20)
        if status:
            convos = [c for c in convos if c.get("state") == status]
        date_from, date_to = _iso_date(args, "date_from"), _iso_date(args, "date_to")
        if date_from:
            convos = [c for c in convos if (c.get("last_at") or "")[:10] >= date_from]
        if date_to:
            convos = [c for c in convos if (c.get("last_at") or "")[:10] <= date_to]
        out = [_convo_row(c) for c in convos[:_limit(args)]]
    else:
        # The own-mailbox lens applies to free text too (M35): a search for
        # "alarm" must not return snippets from the owner's mailbox.
        rows = _their_mailbox_only(tenant_id, client, member, db.search_email_log(
            tenant_id, q=_s(args, "query"), contact=_s(args, "contact"),
            date_from=_iso_date(args, "date_from"), date_to=_iso_date(args, "date_to"),
            status=_s(args, "status", 20), category=None,
            limit=_limit(args), client=client))
        out = [{
            "contact_email": r.get("from_address"),
            "contact_name": r.get("from_name") or r.get("contact_name"),
            "company": r.get("contact_company"),
            "subject": r.get("subject"),
            "snippet": _snip(r.get("body_in"), 200),
            "status": r.get("status"),
            "category": r.get("category"),
            "has_draft_waiting": r.get("status") == "pending",
            "summary": r.get("request_summary"),
            "date": r.get("created_at"),
        } for r in rows]
    suppressed = _suppressed_summary(tenant_id, client, member)
    extra = {"suppressed_contacts": suppressed} if suppressed else {}
    return {**extra, "conversations": out, "count": len(out)}


def _rescue_contact(tenant_id, client, contact: str) -> str | None:
    """Live 2026-09-06 (pass 2): the model batched get_conversation with a
    GUESSED address ("dan.hallam@hallam-co.com") in the same round as the
    search_contacts that would have found the real one — an empty result and
    a wasted round. When the address matches nothing, read it as a name
    ("dan hallam", or a bare name with no @) and resolve it against the CRM;
    exactly one match → that contact's real email. Ambiguity stays honest."""
    raw = contact.split("@", 1)[0] if "@" in contact else contact
    words = [w for w in re.split(r"[.\-_+\s]+", raw) if len(w) > 1 and not w.isdigit()]
    if not words:
        return None
    seen: dict[str, dict] = {}
    for q in (" ".join(words), *words[:2]):
        for c in db.crm_list_contacts(tenant_id, q=q, limit=5, client=client) or []:
            if c.get("email"):
                seen[c["email"].lower()] = c
        if len(seen) == 1:
            break
    if len(seen) != 1:
        return None
    email = next(iter(seen))
    return email if email != contact.lower() else None


def _get_conversation(tenant_id, client, args, member=None) -> dict:
    contact = _s(args, "contact", 254)
    if not contact:
        return {"error": "contact (email address) is required"}
    rows = _their_mailbox_only(
        tenant_id, client, member, db.get_conversation(tenant_id, contact, client=client))
    resolved = None
    if not rows:
        resolved = _rescue_contact(tenant_id, client, contact)
        if resolved:
            rows = _their_mailbox_only(
                tenant_id, client, member,
                db.get_conversation(tenant_id, resolved, client=client))
            contact = resolved
    rows = rows[-20:]   # newest 20 exchanges is plenty of evidence
    if not rows:
        return {"contact": contact, "messages": [], "count": 0,
                "message": ("No conversation for that address. If you guessed it, "
                            "search_contacts by name gives the real one.")}
    return {"contact": contact,
            **({"resolved_from_name": True,
                "note": f"'{args.get('contact')}' matched no conversation; this is "
                        f"the one contact whose name matches — {contact}."} if resolved else {}),
            "messages": [{
        "date": r.get("created_at"),
        "subject": r.get("subject"),
        "customer_wrote": _snip(r.get("body_in")),
        "agent_reply": _snip(r.get("draft_reply")),
        "reply_status": r.get("status"),
        "booking_details": r.get("booking_details") or None,
    } for r in rows], "count": len(rows)}


def _search_contacts(tenant_id, client, args) -> dict:
    q = _s(args, "query")
    rows = db.crm_list_contacts(tenant_id, q=q, limit=_limit(args), client=client)
    words = [w for w in re.split(r"\s+", (q or "").lower()) if len(w) > 1]
    if q and not rows and len(words) > 1:
        # A full name where only part is on file: "Laura Finch" is saved as
        # name "Laura", email laura.finch@… (live 2026-09-23 — the concierge
        # said "I can't find anyone called Laura Finch"). Each word may match
        # the name OR the email.
        wide = db.crm_list_contacts(tenant_id, q=words[0], limit=MAX_ROWS, client=client)
        rows = [r for r in wide
                if all(w in f"{r.get('name') or ''} {r.get('email') or ''}".lower()
                       for w in words)][:_limit(args)]
    if q and not rows:
        # A stated first name remains the display name. Older sender headers
        # and self-stated names still identify the same email contact.
        rows = db.crm_contacts_by_known_name(tenant_id, q, limit=_limit(args), client=client)
    if q:
        # A name may be a dependant's (a pet, a car, a child), not a person's —
        # its owner comes back with what matched.
        try:
            seen = {r.get("id") for r in rows}
            rows += [r for r in db.crm_contacts_by_dependant_name(
                tenant_id, q, limit=_limit(args), client=client) if r.get("id") not in seen]
        except Exception:
            log.warning("search_contacts: dependant match failed", exc_info=True)
    return {"contacts": [{
        "contact_id": r.get("id"),
        "name": r.get("name"),
        "email": r.get("email"),
        "phone": r.get("phone"),
        "company": (r.get("crm_companies") or {}).get("name"),
        "last_activity": r.get("last_activity_at"),
        "sentiment": r.get("sentiment"),
        **({"matched_dependant": r["matched_dependant"]} if r.get("matched_dependant") else {}),
    } for r in rows], "count": len(rows)}


# How the timeline reads to the model — raw activity_type values are storage
# vocabulary, not English.
_TIMELINE_LABELS = {
    "email": "email", "sms": "SMS", "calendar_event": "booking",
    "invoice": "invoice sent", "invoice_paid": "invoice paid",
    "invoice_reminder": "invoice chased", "quote": "quote sent",
    "quote_reminder": "quote chased", "portal_chat": "portal question",
    "reschedule_request": "portal reschedule request",
    "note": "note",
}


def _get_contact_timeline(tenant_id, client, args) -> dict:
    email = (_s(args, "contact", 254) or "").lower()
    if "@" not in email:
        return {"error": "bad_contact",
                "message": ("Pass the contact's email address — "
                            "search_contacts finds it from a name.")}
    matches = db.crm_list_contacts(tenant_id, q=email, limit=1, client=client)
    if not matches:
        return {"found": False,
                "message": "No CRM contact matches that email address."}
    c = matches[0]
    acts = db.crm_activities_for_contact(c["id"], tenant_id,
                                         limit=_limit(args), client=client)
    return {
        "contact": {"name": c.get("name"), "email": c.get("email"),
                    "company": (c.get("crm_companies") or {}).get("name"),
                    "sentiment": c.get("sentiment"),
                    "last_activity": c.get("last_activity_at")},
        "timeline": [{
            "when": (a.get("occurred_at") or "")[:16],
            "type": _TIMELINE_LABELS.get(a.get("activity_type"),
                                         a.get("activity_type")),
            "direction": a.get("direction"),
            "subject": a.get("subject"),
            "summary": a.get("summary"),
            "sentiment": a.get("sentiment"),
        } for a in acts],
        "count": len(acts),
    }


def _list_deals(tenant_id, client, args) -> dict:
    rows = db.crm_list_deals(tenant_id, client=client) or []
    status = _s(args, "status", 20) or "open"
    rows = [r for r in rows if (r.get("status") or "open") == status]
    stage = (_s(args, "stage", 60) or "").lower()
    if stage:
        rows = [r for r in rows
                if stage in ((r.get("crm_stages") or {}).get("name") or "").lower()]
    stale = 0
    try:
        stale = max(0, int((args or {}).get("stale_after_days") or 0))
    except (TypeError, ValueError):
        pass
    out = []
    now = datetime.now(timezone.utc)
    for r in rows[:_limit(args)]:
        contact = r.get("crm_contacts") or {}
        last = r.get("updated_at") or r.get("opened_at") or r.get("created_at")
        days_quiet = None
        if last:
            try:
                days_quiet = (now - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).days
            except ValueError:
                pass
        if stale and (days_quiet is None or days_quiet < stale):
            continue
        out.append({
            "deal_id": r.get("id"),
            "title": r.get("title"),
            "stage": (r.get("crm_stages") or {}).get("name"),
            "status": r.get("status"),
            "value": r.get("value_estimate"),
            "currency": r.get("currency"),
            "contact_id": contact.get("id"),
            "contact_name": contact.get("name"),
            "contact_email": contact.get("email"),
            "company": (r.get("crm_companies") or {}).get("name"),
            "opened": r.get("opened_at"),
            "days_since_activity": days_quiet,
        })
    return {"deals": out, "count": len(out)}


def _get_pipeline_summary(tenant_id, client, args) -> dict:
    rows = db.crm_report_pipeline(tenant_id, client=client) or []
    return {"stages": [{
        "stage": r.get("stage_name"),
        "type": r.get("stage_type"),
        "open_deals": r.get("deal_count"),
        "total_value": r.get("total_value"),
        "avg_days_in_stage": r.get("avg_days_in_stage"),
    } for r in rows]}


def _list_followups(tenant_id, client, args) -> dict:
    limit = _limit(args)
    promises = db.crm_open_promises(tenant_id, client=client) or []
    tasks = db.crm_list_tasks(tenant_id, status="open", client=client) or []
    due_before = _iso_date(args, "due_before")
    if due_before:
        tasks = [t for t in tasks if t.get("due_date") and str(t["due_date"]) <= due_before]
    return {
        "promises": [{
            "direction": "we owe them" if p.get("direction") == "we_owe" else "they owe us",
            "description": _snip(p.get("description"), 200),
            "due": p.get("due_hint"),
            "contact_id": p.get("contact_id"),
            "since": p.get("created_at"),
        } for p in promises[:limit]],
        "tasks": [{
            "title": t.get("title"),
            "due_date": t.get("due_date"),
            "priority": t.get("priority"),
            "contact_id": t.get("contact_id"),
            "deal_id": t.get("deal_id"),
        } for t in tasks[:limit]],
        "open_promises": len(promises), "open_tasks": len(tasks),
    }


def _list_bookings(tenant_id, client, args, member=None) -> dict:
    status = _s(args, "status", 20)
    contact = _s(args, "contact")
    if contact and "@" not in contact:
        # A booking's contact column holds the address, so a name only matched when the address
        # happened to contain it (rig 14 Oct, N8: "Leah" matched through an alias address).
        # Resolve the name the way get_contact_record does; nobody found keeps the raw string.
        name = "".join(ch for ch in contact if ch not in ",()*%\\").strip()
        try:
            found, candidates = concierge_reads._resolve_contact(tenant_id, client, name) if name else (None, [])
        except Exception:
            log.warning("list_bookings: contact lookup failed", exc_info=True)
            found, candidates = None, []
        if found is not None:
            contact = found.get("email") or contact
        elif candidates:
            # The candidates are CRM records, read through the diary door: only someone who could ask
            # the CRM tools the same question, over the whole book, is told who they are.
            from app import crm_visibility, people
            if people.can_open(member, "crm") and not crm_visibility.narrows_contacts(member):
                return {"bookings": [], "count": 0,
                        "which": [{"name": c.get("name"), "email": c.get("email"), "contact_id": c.get("id")}
                                  for c in candidates],
                        "message": "Several contacts match — ask which one."}
    date_to = _iso_date(args, "date_to")
    if status == "completed":
        # Past bookings stay "confirmed" (nothing marks them completed), so the
        # honest filter is confirmed-before-today (live 2026-09-06: two empty
        # calls and "the system doesn't appear to have historical records").
        from datetime import date as _date, timedelta as _td
        yesterday = (_date.today() - _td(days=1)).isoformat()
        status = "confirmed"
        date_to = min(date_to, yesterday) if date_to else yesterday
    date_from = _iso_date(args, "date_from")
    rows = db.search_bookings(
        tenant_id, date_from=date_from, date_to=date_to,
        contact=contact, status=status,
        limit=_limit(args), client=client)
    # A stay is in on every day it covers (live 2026-09-23, kennels: "which
    # dogs are in on the 13th?" → "nothing booked" while Rufus (9th→13th) and
    # Bella (12th→26th) were both in — the range keys on the day it STARTED).
    # The live diary's stays that began before the range and are still running
    # at its start join the answer, marked as running into it.
    running = []
    if date_from and not status:
        start_abs = slotgrid.abs_block(date_from, 0)
        seen = {r.get("id") for r in rows}
        try:
            for r in db.stays_running_into(tenant_id, date_from, contact=contact,
                                           client=client):
                span = slotgrid.stay_span(r)
                if span and r.get("id") not in seen and span[0] + span[1] > start_abs:
                    running.append(r)
        except Exception:
            log.warning("list_bookings: running stays read failed", exc_info=True)
    all_rows = running[::-1] + list(rows)
    # A named contact with nothing live: the cancelled ones are what they are after (rig 14 Oct,
    # "that job that got scrapped": a second round to ask for status=cancelled). Only this empty
    # case, so a date-only diary question never lists cancelled rows.
    fell_back = False
    if contact and not status and not all_rows:
        gone = db.search_bookings(
            tenant_id, date_from=date_from, date_to=date_to,
            contact=contact, status="cancelled",
            limit=_limit(args), client=client)
        if gone:
            rows = all_rows = list(gone)
            fell_back = True
    # Who's on each job, and what kind of work it is (plan steps 2 + 4): a
    # staff member's lookup is narrowed exactly as their Diary page is, and
    # `person` answers "what's Em doing" / "my jobs".
    from app import job_people, people as _people
    cfg = {}
    try:
        cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    except Exception:
        pass
    job_people.annotate(tenant_id, all_rows)
    work_labels = {k["key"]: k["label"] for k in _people.kinds_for(cfg)}
    if member is not None and _people.does(member):
        all_rows = [r for r in all_rows if _people.owns_booking(member, r, cfg)]
    who = _s(args, "person", 80)
    if who:
        roster = job_people.team(tenant_id)
        t = job_people.find_person(roster, who)
        if not t:
            return {"bookings": [], "count": 0,
                    "note": f"Nobody called '{who}' is on the team — "
                            "the team is: " + ", ".join(p["name"] for p in roster)}
        on = {b for b, ps in job_people.by_booking(
            tenant_id, [r.get("id") for r in all_rows], roster).items()
            if any(p["person_id"] == t["id"] for p in ps)}
        theirs = set(t["does"])
        all_rows = [r for r in all_rows if str(r.get("id")) in on
                    or _people.kind_of(r, cfg) in theirs]
    keep = {id(r) for r in all_rows}
    running = [r for r in running if id(r) in keep]
    rows = [r for r in rows if id(r) in keep]
    subjects = db.subjects_for_bookings(tenant_id, [r["id"] for r in all_rows if r.get("id")],
                                        client=client) if all_rows else {}

    def _row(r, runs_in: bool) -> dict:
        out = {
            # booking_id is what move_booking proposals must reference — its absence
            # here forced the model to guess with the human ref (live bug 2026-07-26).
            "booking_id": r.get("id"),
            "date": r.get("booking_date"),
            "window": r.get("window"),
            "contact": r.get("contact"),
            "what": r.get("summary"),
            "status": r.get("status"),
            "reference": r.get("booking_ref"),
            "quoted_amount": r.get("quoted_amount"),
        }
        span = slotgrid.stay_span(r)
        if span:
            arr = slotgrid.abs_to_datetime(span[0])
            dep = slotgrid.abs_to_datetime(span[0] + span[1])
            nights = dep.date().toordinal() - arr.date().toordinal()
            if nights > 0:      # a stay: say when it runs, not just the day it began
                out["arrives"] = arr.strftime("%a %d %b %Y %H:%M")
                out["leaves"] = dep.strftime("%a %d %b %Y %H:%M")
                out["nights"] = nights
        names = [str(i.get("name") or "").strip() for i in subjects.get(r.get("id"), [])]
        if any(names):
            out["for"] = [n for n in names if n]
        if runs_in:
            out["started_before_range"] = True
        work = work_labels.get(_people.kind_of(r, cfg))
        if work:
            out["work"] = work
        if r.get("on_it"):
            out["on_it"] = r["on_it"]
        return out

    if fell_back and rows:
        return {"bookings": [], "count": 0, "cancelled": [_row(r, False) for r in rows],
                "note": "No live bookings for this contact; their cancelled bookings are listed under cancelled."}
    if fell_back:
        return {"bookings": [], "count": 0}
    return {"bookings": [_row(r, True) for r in running[::-1]] + [_row(r, False) for r in rows],
            "count": len(all_rows)}


def _list_invoices(tenant_id, client, args) -> dict:
    rows = db.list_invoices(tenant_id, client=client) or []
    today = datetime.now(timezone.utc).date().isoformat()

    def _derived_status(r) -> str:
        # 'overdue' is partly derived: a sent invoice past its due date counts even
        # before the daily job stamps it (the owner asks in real time).
        if (r.get("status") in ("sent", "partially_paid")
                and (r.get("due_date") or "9999") < today):
            return "overdue"
        return r.get("status") or "draft"

    status = _s(args, "status", 20)
    contact = (_s(args, "contact", 120) or "").lower()
    out = []
    for r in rows:
        ds = _derived_status(r)
        if status and ds != status and r.get("status") != status:
            continue
        if contact and contact not in ((r.get("customer_email") or "").lower()
                                       + " " + (r.get("customer_name") or "").lower()):
            continue
        days_overdue = None
        if ds == "overdue" and r.get("due_date"):
            try:
                days_overdue = (datetime.now(timezone.utc).date()
                                - datetime.strptime(str(r["due_date"])[:10], "%Y-%m-%d").date()).days
            except ValueError:
                pass
        out.append({
            # invoice_id is what every invoice action must reference (same contract
            # as booking_id on list_bookings — never let the model guess).
            "invoice_id": r.get("id"),
            "ref": r.get("invoice_ref"),
            "customer": r.get("customer_name") or r.get("customer_email"),
            "customer_email": r.get("customer_email"),
            "total": r.get("total"),
            "status": ds,
            "issue_date": r.get("issue_date"),
            "due_date": r.get("due_date"),
            "days_overdue": days_overdue,
            "paid_at": r.get("paid_at"),
            "payment_method": r.get("payment_method"),
            "reminders_sent": r.get("reminders_sent"),
            "viewed_by_customer": bool(r.get("viewed_at")),
        })
        if len(out) >= _limit(args):
            break
    # A DRAFT's lines ride along so a change to it can be written as the full new set
    # (invoice_edit_draft replaces the lines, as the main app's editor does). A sent
    # invoice's lines are not offered: nothing here edits one.
    drafts = [o for o in out if o["status"] == "draft"]
    if drafts:
        try:
            by_invoice = db.invoice_lines_by_invoice(
                tenant_id, [d["invoice_id"] for d in drafts if d["invoice_id"]], client=client)
        except Exception:
            log.warning("draft invoice lines read failed (tenant=%s)", tenant_id, exc_info=True)
            by_invoice = {}
        for d in drafts:
            d["lines"] = [{"description": l.get("description"), "amount": l.get("amount")}
                          for l in by_invoice.get(d["invoice_id"], [])][:20]
    return {"invoices": out, "count": len(out)}


def _list_quotes(tenant_id, client, args) -> dict:
    rows = db.list_quotes(tenant_id, client=client) or []
    today = datetime.now(timezone.utc).date().isoformat()

    def _derived_status(r) -> str:
        # 'expired' is partly derived: a sent quote past its valid-until date
        # counts even before the daily job flips it (mirror of list_invoices'
        # derived 'overdue').
        if r.get("status") == "sent" and (r.get("valid_until") or "9999") < today:
            return "expired"
        return r.get("status") or "draft"

    status = _s(args, "status", 20)
    contact = (_s(args, "contact", 120) or "").lower()
    out = []
    for r in rows:
        ds = _derived_status(r)
        if status and ds != status and r.get("status") != status:
            continue
        if contact and contact not in ((r.get("customer_email") or "").lower()
                                       + " " + (r.get("customer_name") or "").lower()):
            continue
        out.append({
            # quote_id is what every quote action must reference (the
            # invoice_id / booking_id contract — never let the model guess).
            "quote_id": r.get("id"),
            "ref": r.get("quote_ref"),
            "customer": r.get("customer_name") or r.get("customer_email"),
            "customer_email": r.get("customer_email"),
            "title": r.get("title"),
            "total": r.get("total"),
            "status": ds,
            "issue_date": r.get("issue_date"),
            "valid_until": r.get("valid_until"),
            "viewed_by_customer": bool(r.get("viewed_at")),
            "times_viewed": r.get("view_count"),
            "chases_sent": r.get("reminders_sent"),
            "accepted_by": r.get("accepted_name"),
            "accepted_at": r.get("accepted_at"),
            "decline_reason": r.get("decline_reason"),
            "deposit_percent": r.get("deposit_percent"),
        })
        if len(out) >= _limit(args):
            break
    # A DRAFT's lines ride along (see _list_invoices): quote_edit_draft replaces them
    # as a full set, and an optional line is marked so it is not dropped by accident.
    drafts = [o for o in out if o["status"] == "draft"]
    if drafts:
        try:
            by_quote = db.quote_lines_by_quote(
                tenant_id, [d["quote_id"] for d in drafts if d["quote_id"]], client=client)
        except Exception:
            log.warning("draft quote lines read failed (tenant=%s)", tenant_id, exc_info=True)
            by_quote = {}
        for d in drafts:
            d["lines"] = [{"description": l.get("description"), "amount": l.get("amount"),
                           **({"optional": True} if l.get("is_optional") else {})}
                          for l in by_quote.get(d["quote_id"], [])][:20]
    return {"quotes": out, "count": len(out)}


async def _search_knowledge_base(tenant_id, client, args) -> dict:
    query = _s(args, "query", 300)
    if not query:
        return {"error": "query is required"}
    # kb.retrieve, not kb.search_chunks: retrieve retries embedding 429s with
    # backoff and returns [] instead of raising (the Voyage free-tier rate limit
    # silently emptied searches before — see redteam Gate A). Semantic match, so
    # "annual service" finds "Annual service pricing" without term gymnastics.
    chunks = await kb.retrieve(tenant_id, query)
    if not chunks and not kb.is_configured():
        return {"entries": [], "note": "the knowledge base is not configured"}
    return {"entries": [{
        "source": c.get("source"),
        "content": _snip(c.get("content")),
    } for c in chunks[:5]]}


def _list_awaiting_approval(tenant_id, client, args, member=None, user_id=None) -> dict:
    # The asker's own work (src/app/concierge_ledger.py): their tasks, their cards, the
    # emails drafted for them. Whose it is comes from the member row, else the
    # signed-in user the route passes; with neither, nothing is shown.
    from app import concierge_ledger
    return concierge_ledger.ledger(tenant_id, client, args, member, user_id)


def _get_product_help(tenant_id, client, args, member=None):
    from app import platform_help
    return platform_help.lookup(args.get('query',''),
        platform_help.runtime_context(tenant_id,client,member),all_features=args.get('all') is True)


_EXECUTORS = {
    "get_product_help": _get_product_help,
    "get_inbox_overview": _get_inbox_overview,
    "search_conversations": _search_conversations,
    "get_conversation": _get_conversation,
    "search_contacts": _search_contacts,
    "get_contact_timeline": _get_contact_timeline,
    "list_deals": _list_deals,
    "get_pipeline_summary": _get_pipeline_summary,
    "list_followups": _list_followups,
    "list_bookings": _list_bookings,
    "search_knowledge_base": _search_knowledge_base,
    "list_invoices": _list_invoices,
    "list_quotes": _list_quotes,
    "get_availability": lambda t, c, a: concierge_reads.get_availability(t, c, a),
    "get_contact_record": lambda t, c, a: concierge_reads.get_contact_record(t, c, a),
    "list_dormant_contacts": lambda t, c, a: concierge_reads.list_dormant_contacts(t, c, a),
    "list_expiring": lambda t, c, a: concierge_reads.list_expiring(t, c, a),
    "list_awaiting_approval": _list_awaiting_approval,
}


# Which CRM payloads name a customer, and where. A tool at the CRM door that
# is NOT listed here returns nothing customer-shaped; if that stops being true,
# add it, because the filter below is the only thing standing between a staff
# login and the whole book.
_CUSTOMER_LISTS = {"search_contacts": "contacts"}
_ONE_CUSTOMER = ("get_contact_timeline", "get_contact_record")


# Tools whose executor is handed the person, because what they return depends
# on WHOSE mailbox it is rather than merely whether a door is open. Filtering
# these after the fact is not possible: the rows are already folded into a
# shape that has lost the mailbox by then.
_MEMBER_AWARE = frozenset(("get_inbox_overview", "search_conversations", "get_conversation",
                           # narrowed like the Diary page (plan step 4, G4)
                           "list_bookings", "get_product_help",
                           # the asker's own cards
                           "list_awaiting_approval"))


# Tools that are also told the signed-in user's id: a member row may be absent (an
# account that predates People) while the person asking is still known.
_SIGNED_IN_AWARE = frozenset(("list_awaiting_approval",))


def _their_mailbox_only(tenant_id, client, member, rows, key="mailbox_id"):
    """Rows from mailboxes that are this person's own. Mail with no mailbox on
    it belongs to the DEFAULT one — the oldest, which is the owner's — so
    nothing legacy becomes theirs by accident (Kris 2026-09-14). Best-effort in
    ONE direction: a read that fails returns NOTHING rather than everything."""
    from app import people
    if not people.inbox_is_theirs_only(member):
        return list(rows or [])
    try:
        boxes = db.list_mailboxes(tenant_id, client=client)
        default_id = boxes[0]["id"] if boxes else None
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "mailbox lens failed (tenant=%s)", tenant_id, exc_info=True)
        return []
    return [r for r in (rows or [])
            if people.may_read_mailbox(member, (r or {}).get(key), default_id)]


def allowed_customers(tenant_id, client, member) -> set[str]:
    """The addresses behind this person's own work (the customer lens). Raises
    when it can't be read — every caller refuses rather than widens."""
    from app import crm_visibility
    from app import db as _db
    from datetime import date as _date, timedelta as _td
    since = (_date.today() - _td(days=365)).isoformat()
    rows = _db.list_booking_contacts(tenant_id, since, client=client)
    cfg = _db.get_scheduling_config(tenant_id, client=client)
    return crm_visibility.theirs(member, rows, cfg)


def _their_customers_only(tenant_id, client, name: str, out, member):
    """Narrow a CRM answer to what this person may read. Two lenses, applied
    separately (security review 2026-09, M33): WHOSE customers (staff with
    kinds delegated see the customers behind their own work) and WHICH
    fields (every staff member without "See private details" loses the
    private ones, delegated or not). Best-effort in one direction only: if a
    lens can't be read the answer is REFUSED or emptied, never widened."""
    if not isinstance(out, dict) or member is None or tool_door(name) != "crm":
        return out
    from app import crm_visibility
    if crm_visibility.narrows_contacts(member):
        try:
            allowed = allowed_customers(tenant_id, client, member)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "concierge crm lens failed (tenant=%s)", tenant_id, exc_info=True)
            return NOT_THEIRS
        key = _CUSTOMER_LISTS.get(name)
        if key:
            kept = [r for r in (out.get(key) or [])
                    if str(r.get("email") or "").strip().lower() in allowed]
            return {**out, key: kept, "count": len(kept)}
        if name in _ONE_CUSTOMER:
            who = out.get("contact") if isinstance(out.get("contact"), dict) else out
            if not out.get("found", True) and "which" not in out:
                return out                       # "no such contact" is not a leak
            addr = str((who or {}).get("email") or "").strip().lower()
            if out.get("which"):                 # the "several match" list
                kept = [c for c in out["which"]
                        if str(c.get("email") or "").strip().lower() in allowed]
                return {**out, "which": kept} if kept else NOT_THEIRS
            if addr and addr not in allowed:
                return NOT_THEIRS
    # Their customer — but the private fields still are not theirs.
    return _their_fields_only(tenant_id, client, name, out, member)


def _shut_line(line, shut: set[str]) -> bool:
    """Is this expiry line ("Alarm certificate: lapses …") about a shut field?"""
    s = str(line or "")
    return any(s.startswith(f"{label}:") for label in shut)


def _their_fields_only(tenant_id, client, name: str, out, member):
    """The FIELD lens: private field values, expiry lines and dependant facts
    removed for a staff member who hasn't been given "See private details".
    Nothing changes for an owner, a manager or staff who hold the keys."""
    from app import crm_visibility
    if not isinstance(out, dict) or not crm_visibility.hides_private(member):
        return out
    from app import db as _db
    if name in _ONE_CUSTOMER:
        if isinstance(out.get("fields"), list) or isinstance(out.get("expiring"), list):
            try:
                defs = _db.crm_list_field_definitions(tenant_id, client=client) or []
                shut = {str(d.get("label")) for d in defs
                        if not crm_visibility.can_see_field(member, d)}
                if isinstance(out.get("fields"), list):
                    out = {**out, "fields": [f for f in out["fields"]
                                             if str(f.get("label")) not in shut]}
                if isinstance(out.get("expiring"), list):
                    out = {**out, "expiring": [ln for ln in out["expiring"]
                                               if not _shut_line(ln, shut)]}
            except Exception:
                # cannot tell which are private: show none of them
                out = {**out, **({"fields": []} if isinstance(out.get("fields"), list) else {}),
                       **({"expiring": []} if isinstance(out.get("expiring"), list) else {})}
        if isinstance(out.get("dependants"), list):
            out = {**out, "dependants": _redact_dependants(
                tenant_id, client, out["dependants"], member)}
    if name == "list_expiring" and isinstance(out.get("expiring"), list):
        try:
            from app import requirement_groups as _rg
            cdefs = {str(d.get("label")): d for d in (_db.crm_list_field_definitions(
                tenant_id, entity="contact", client=client) or [])}
            groups = {str(g.get("label")): g for g in
                      _rg.clean_groups(_db.list_requirement_groups(tenant_id, client=client))}
        except Exception:
            return {**out, "expiring": [], "count": 0}
        kept = []
        for r in out["expiring"]:
            detail = str(r.get("detail") or "")
            if r.get("what") == "Customer":
                defn = cdefs.get(detail, {"label": detail, "key": detail})
            else:
                fields = (groups.get(str(r.get("what"))) or {}).get("fields") or []
                defn = next((f for f in fields if str(f.get("label")) == detail),
                            {"label": detail, "key": detail})
            if crm_visibility.can_see_field(member, defn):
                kept.append(r)
        dropped = len(out["expiring"]) - len(kept)
        out = {**out, "expiring": kept,
               "count": max(0, int(out.get("count") or 0) - dropped)}
    return out


def _redact_dependants(tenant_id, client, rows, member):
    """A dependant's facts, minus the fields this person doesn't hold.

    The payload names its group by LABEL ("Dog") and its facts by field KEY
    ("gate_code"), so the lookup takes both — and a fact whose field cannot be
    found at all is still judged, on its own key, rather than passed through."""
    from app import crm_visibility
    try:
        from app import db as _db, requirement_groups as _rg
        groups = {str(g.get("label")): g for g in
                  _rg.clean_groups(_db.list_requirement_groups(tenant_id, client=client))}
    except Exception:
        return [{**r, "facts": {}} for r in rows]     # cannot tell: show none
    out = []
    for r in rows:
        g = groups.get(str(r.get("kind")))
        fields = ((g or {}).get("fields") or [])
        by_name = {str(f.get("key")): f for f in fields}
        by_name.update({str(f.get("label")): f for f in fields})
        facts = {k: v for k, v in (r.get("facts") or {}).items()
                 if crm_visibility.can_see_field(member, by_name.get(str(k), {"label": k, "key": k}))}
        out.append({**r, "facts": facts})
    return out


async def execute(tenant_id: str, client, name: str, args: dict, *,
                  full_qa: bool, member: dict | None = None, user_id: str | None = None) -> dict:
    """Run one tool call, tenant-scoped, tier-gated and door-gated. Never
    raises — a data error degrades to an honest payload the model can relay.

    The door is checked HERE as well as in the catalogue: hiding a tool from
    the model is not the same as refusing it, and a model that has seen the
    name once in a long conversation can still call it."""
    if name not in TOOL_NAMES:
        return {"error": f"unknown tool: {name}"}
    if not full_qa and name in PRO_TOOLS:
        return LOCKED
    door = tool_door(name)
    if member is not None and door is not None:
        from app import crm_visibility, people
        if not people.can_open(member, door):
            return CLOSED_DOOR
        if name in WHOLE_BOOK_TOOLS and crm_visibility.narrows_contacts(member):
            return NOT_THEIRS
    try:
        import inspect
        import asyncio
        executor = _EXECUTORS[name]
        arguments = (tenant_id, client, args or {}, member) if name in _MEMBER_AWARE else (tenant_id, client, args or {})
        if name in _SIGNED_IN_AWARE:
            arguments += (user_id,)
        out = executor(*arguments) if inspect.iscoroutinefunction(executor) else await asyncio.to_thread(executor, *arguments)
        out = await out if inspect.isawaitable(out) else out
        return await asyncio.to_thread(_their_customers_only, tenant_id, client, name, out, member)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "concierge tool %s failed (tenant=%s)", name, tenant_id, exc_info=True)
        return {"error": "lookup_failed",
                "message": "That lookup failed — tell the customer you couldn't "
                           "check this just now, and answer from what you have."}
