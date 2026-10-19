"""Concierge settings recommendations — the machine half of "Help me set this up".

The help assistant (app/concierge.answer_help) can attach a `recommend` payload to a
reply: a settings PAGE plus a list of {name, value} pairs. The widget renders it as a
"Use recommended settings" card; clicking fills the page's real form for the customer
to review and save (diary settings / booking details), or — for CRM settings, whose
page is per-row forms — applies via POST /concierge/crm-apply and lands them on the
page to review and edit. NOTHING here writes; this module is the contract:

  * PAGES — the catalogue of recommendable pages and their allowed field names,
    injected into the help prompt so the model can only speak in real form fields;
  * clean() — server-side validation of a model recommendation (whitelist names,
    trim, cap, dedupe) so the widget only ever sees well-formed pairs;
  * the pipe-format parsers for the repeatable rows ("Name | 60 | 25").

Field names match the settings templates 1:1 (src/app/templates/app/_scheduling_settings
.html, diary_intake.html, crm_settings.html) EXCEPT the four repeatable row kinds
(service / resource / field / stage), which use one entry per row in a "a | b | c"
value — easier for the model than JSON-in-a-string, and readable in the chat card.
"""
import logging
import re
from datetime import date, timedelta

log = logging.getLogger("blautom8.settings_recommend")

MAX_ENTRIES = 40
MAX_VALUE = 200
MAX_ROW = 400      # a repeatable row carries several "|" parts, so it gets more room
# Fields the owner writes as prose need room for it: a rule that rejects an
# over-long value must not swallow an ordinary paragraph (a knowledge note, the
# payment terms). Everything else keeps the short limits above.
PROSE_LIMITS = {("knowledge", "note"): 1500, ("invoice_settings", "payment_terms"): 600,
                ("invoice_settings", "footer_note"): 400}

# Weekday codes shared by working_days and the per-day hour overrides.
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAYHOURS_RE = re.compile(r"^dayhours_(mon|tue|wed|thu|fri|sat|sun)_(open|close)$")

PAGES = {
    "diary_settings": {
        "label": "Diary settings",
        "href": "/app/diary/settings",
        "repeat": {"service", "resource", "service_urgent", "service_out_of_hours",
                   "service_after_job"},
        "fields": {
            "open_time": "opening time, 24h HH:MM",
            "close_time": "closing time, 24h HH:MM",
            "slot_minutes": "slot length in minutes — one of 15, 30, 45, 60, 90, 120, 180, 240, 360, 480",
            "working_days": "comma-separated day codes from mon,tue,wed,thu,fri,sat,sun",
            "blackout_dates": "dates never bookable, YYYY-MM-DD one per line",
            "min_notice_hours": "minimum booking notice in hours, 0-336",
            "horizon_days": (
                "how far ahead bookings open, 1-365 days; use the owner's chosen horizon. A date beyond it "
                "reads as not open yet"
            ),
            "capacity_per_window": "bookings that can share one slot, 1-99",
            "stay_mode": (
                "1 when one booking can span days so a departure/collection time is captured "
                "(arrival-to-departure bookings), else 0. stay_mode 1 CAPTURES the departure/collection time "
                "itself — never ALSO write a booking_details field row for a collection, departure, pick-up or "
                "return time, or the agent asks the customer for it twice"
            ),
            "windows": "booking unit when no services are set — slots, ampm or day",
            "buffer_minutes": "gap between bookings in minutes; 0 means no gap",
            "service": (
                "REPEATABLE, one per service — \"Name | minutes | price | timing\" (price optional; timing is "
                "time, half or day: time = an exact start time; half = a morning or afternoon arrival window; "
                "day = a whole-day booking). One row for EVERY service the owner names or prices, including a "
                "separately priced extra. Use only the owner's stated duration, price and timing; ask when "
                "these are missing. Optional named parts: 'resource: Name' links to a resource on this card "
                "or already saved on the page; 'days: mon-fri' restricts the service to the stated weekdays. "
                "Include these only when the owner states the link or days"
            ),
            "resource": "REPEATABLE, one per bookable unit type — \"Name | how many | rate | per\" (rate optional; per one of booking,hour,day,night,week)",
            "stay_rate": "price per night/day/week for stays, number",
            "stay_rate_unit": "night, day or week",
            "stay_deposit": (
                "deposit to hold a stay as an AMOUNT in pounds (optional). Never enter a percentage here: a "
                "percentage belongs in default_deposit_percent on invoice_settings. Use only the owner's "
                "stated amount"
            ),
            "stay_min_nights": (
                "minimum nights for an OVERNIGHT stay, 0-365, as specified by the owner; same-day visits are "
                "never bound by it"
            ),
            "checkin_after": (
                "earliest arrival time for an overnight stay, HH:MM; use the owner's stated restriction, "
                "otherwise leave unset"
            ),
            "checkout_before": (
                "latest departure time for an overnight stay, HH:MM; use the owner's stated restriction. An "
                "arrival window is not a departure limit; leave unset if departures are permitted in several "
                "windows"
            ),
            "party_enabled": "1 when bookings count people against capacity, else 0",
            "party_max_people": "max people at once, number",
            "recurring_enabled": "1 to allow standing repeat bookings, else 0",
            "recurring_freq": (
                "comma-separated from weekly,fortnightly,monthly,six_weekly,quarterly (six_weekly = every six "
                "weeks; quarterly = every three months); include only frequencies the owner offers"
            ),
            "recurring_max": "max repeats for a recurring booking, 1-52",
            # The Cancellations & credits face is on THIS page (Diary settings), never
            # the Packages page — run #8 (2026-09-05): the card deferred the four to
            # "the Packages page" and a 48-hour rule sat at the 24-hour default.
            "credits_spend": (
                "CANCELLATIONS & CREDITS — on THIS page, never the packages page. How a package credit is "
                "spent: auto when the session happens, or register when attendance is marked. Set from the "
                "owner's policy when the business sells packages, blocks or passes"
            ),
            "late_cancel_hours": (
                "the business's OWN cancellation window in hours, 0-336. Set it when the owner states a "
                "cancellation rule. This is on Diary settings, not Packages, and is distinct from "
                "min_notice_hours. Never supply an unchosen window"
            ),
            "no_show_spends": (
                "1 when a no-show or late cancellation spends a credit, else 0. Set with late_cancel_hours "
                "from the owner's policy, only for credits/packages; omit for pay-per-booking"
            ),
            "excused_refunds": (
                "1 when an excused absence restores a credit, else 0. Set from the owner's policy, only with "
                "credits/packages; omit for pay-per-booking"
            ),
            "late_move_charged": (
                "1 when moving inside the late-cancellation window charges or spends a credit before a fresh "
                "booking, or 0 when a late move is free. Set with late_cancel_hours from the owner's policy"
            ),
            "hours_unit": (
                "for packages sold in HOURS: minutes deducts the actual session duration in hours; hour "
                "deducts exactly one hour per session regardless of duration. Use the owner's charging policy"
            ),
            "courses_on": (
                "1 when the business runs fixed-start blocks with a set number of places, else 0"
            ),
            "waitlist_on": (
                "1 when the owner offers a waiting list for a full session or slot, else 0"
            ),
            "waitlist_notify": (
                "how a freed place is offered to the first waiting: approve drafts a message for owner "
                "approval; auto sends at once. Use the owner's choice"
            ),
            "offer_hours": "hours to accept an offered waiting-list place before it moves to the next person, 1-336; use the owner's choice",
            "reenrol_days": (
                "days before a block's last session to invite current participants to book again, 0-120; 0 = "
                "never"
            ),
            "reenrol_notify": "how that invitation is sent: approve or auto, as chosen by the owner",
            "waitlist_stage": "the CRM pipeline stage a contact's deal moves to when they join the waiting list — "
                              "ONE of the stages recommended on the crm_settings page (e.g. 'Waiting list'); blank = none",
            "booked_stage": "the CRM pipeline stage the deal moves to when they book from the list — one of the "
                            "crm_settings stages (e.g. 'Enrolled'); blank = none",
            "noun_booking": (
                "the business's own singular word for a booking; use the label the owner supplies"
            ),
            "noun_participant": (
                "the business's own singular word for the person attending; use the label the owner supplies"
            ),
            "makeups_on": (
                "1 when the owner allows a session missed with notice to be made up, else 0"
            ),
            "makeup_per_term": "make-ups per participant per block, 0-12; 0 = no limit. Use the owner's choice",
            "makeup_notice_hours": "hours of notice a missed session needs to earn a make-up, 0-336; use the owner's choice",
            "makeup_within_days": "days after a missed session in which a make-up must be taken, 0-365; 0 = any time. Use the owner's choice",
            "makeup_no_show_ok": "1 when a no-show or late cancellation also earns a make-up, else 0; use the owner's choice",
            "makeup_free": "1 when a make-up is free, 0 when charged as a normal session; use the owner's choice",
            "delivery_offered": (
                "1 only when items are delivered to and collected from the customer, else 0. A service "
                "performed at the customer's location, or collecting a person, is not item delivery; its "
                "address belongs in booking details"
            ),
            "delivery_fee": "delivery fee, number (optional)",
            "delivery_radius": "delivery radius in miles",
            # dayhours_{day}_open / dayhours_{day}_close accepted via _DAYHOURS_RE.
            # Urgent services and out of hours (docs/plan-service-urgency.md, D1
            # stage 4): lifted from the owner's OWN notes, never guessed.
            "service_urgent": (
                "REPEATABLE, one per service the owner offers the SAME DAY — \"Service name | cut-off HH:MM | "
                "notice hours\". Use the exact service name and only the cut-off and notice the owner states; "
                "ask if either is missing"
            ),
            "service_out_of_hours": (
                "REPEATABLE, one per service offered OUTSIDE opening hours — \"Service name | evenings until "
                "HH:MM | extra days | bank holidays yes/no | first-hour price | price an hour after\". Use the "
                "exact service name. Include only hours, days and prices the owner states; ask for missing "
                "values, never infer a closing time or price"
            ),
            # After the job (docs/plan-after-job-tasks.md). Agnostic by rule: the
            # examples span several kinds of business, none of them the default.
            "service_after_job": (
                "REPEATABLE, one per service with work due AFTER completion — \"Service name | Task — N days; "
                "Task — N working days\". Substitute the owner's exact service, task names and deadlines; never "
                "invent them"
            ),
            "job_done_when": "marked or day_end — when a job counts as done, which starts its after-job "
                             "deadlines: marked (the owner or a helper ticks it done; the default) or "
                             "day_end (automatically when its last day ends)",
            "bank_holiday_nation": "england-and-wales, scotland or northern-ireland — from the business's "
                                   "address — when their notes treat bank holidays as closed or out of hours",
        },
    },
    "booking_details": {
        "label": "Booking details",
        "href": "/app/diary/intake",
        "repeat": {"field"},
        "fields": {
            "field": "REPEATABLE, one per detail the AI collects for a booking — \"Label | required or optional | hint for the AI | only for: Service A; Service B | remember: once, confirm or expires\" (hint optional; the 'only for' part optional — give it when the detail matters for SOME of the business's services only, e.g. a detail for Service A but not Service B; service names exactly as listed; leave it as 'only for:' with nothing after when a remember part follows). The REMEMBER part is the row's durability — give it for a fact about the CUSTOMER that holds between visits, so it is kept on their record (it appears under Customer fields) and read back on their next booking, and a repeat customer is asked only what changed: 'remember: once' for a fact that does not change — a licence number, a date of birth, an allergy, a membership number, the address the business travels to when a customer has ONE; 'remember: confirm' for one that usually holds but is worth checking each visit in one line — a contact preference or access arrangement; 'remember: expires' for a DATED certificate or licence that lapses (the date is kept and re-asked once it has). Leave the remember part OUT for what changes every visit — the address for THIS job when a customer has several properties, an inscription, a headcount, a preferred time — and say plainly in the reply which rows are remembered and which are asked each visit. Never a payment or deposit question (invoicing handles money), never a fact that belongs to a dependant the business tracks per named subject (its type, category or other details — those live on the Dependants page), never a count of dependants (the booking names them). A row is a QUESTION the customer answers when booking: a rule about how the business works (who must approve something, what happens after the visit) is never a row, it is a knowledge note. A detail that applies to SOME customers only is OPTIONAL, with a hint saying who it is for. REQUIRED only for what the agent cannot book without (where, when, what); a fact the owner settles at an initial appointment (preferences or other non-blocking details) is OPTIONAL — required means the agent refuses to book until it is answered. Never a row that asks for an alarm code, key-safe number or password — those never travel by email; the hint may say the customer will give access details in person. A document row (a certificate the customer attaches) is never remembered — it is attached per booking. To request one, add the named part 'kind: document' to its row; 'kind: answer' is a question the customer answers.",
        },
    },
    "knowledge": {
        "label": "Knowledge notes",
        "href": "/app/knowledge",
        "repeat": {"note"},
        "fields": {
            "note": (
                "REPEATABLE, one per fact the agent may quote — \"Label | the fact, stated plainly\". Label is a "
                "short title in the owner's words, such as \"Service prerequisite\". Content is ONLY what the "
                "owner said, numbers exact, one topic per note; never a price, rule or hour they did not give"
            ),
        },
    },
    "packages": {
        "label": "Packages",
        "href": "/app/invoices/settings#packages",
        "repeat": {"package"},
        "fields": {
            "package": (
                "REPEATABLE, one per package / block / pass the business sells AHEAD OF USE — \"Name | quantity "
                "| session or hour | price | valid days | services…\" (quantity = how many sessions or hours it "
                "holds; valid days = how long from purchase before the credits expire, 0 = never; services = "
                "which services it covers, semicolon-separated, blank = all). ONLY when the business genuinely "
                "sells prepaid blocks — a defined number of sessions or hours; never for a business that "
                "invoices each job."
            ),
            "packages_paid_only": "1 when only PAID packages count as credits, else 0 (credits normally count from the invoice being issued and read 'unpaid' until paid)",
            "package_low_at": "tell the customer automatically when this many remain, 0 = never (use the owner's choice)",
            "package_expiry_days": "warn the customer this many days before credits expire, 0 = never (use the owner's choice)",
        },
    },
    "dependants": {
        "label": "Dependants",
        "href": "/app/diary/dependants",
        "repeat": {"group", "field", "ladder"},
        # a field is "Kind | Label | …" — the kind alone is not its identity
        "key_parts": {"field": 2, "ladder": 2},
        "fields": {
            "group": (
                "REPEATABLE, one per KIND of subject a booking is about — \"Kind | plural | one or several\". "
                "Use the customer's own singular and plural labels; several means each customer may have more "
                "than one"
            ),
            "field": (
                "REPEATABLE, one per detail of each subject — \"Kind | Label | type | required or optional | "
                "once, confirm or expires | gate or no gate | hint\". TYPE is text, number, date, yes/no, or 'dropdown: Option one, Option two' when the answer is one of a short list the owner names, "
                "matching the answer. An expires field is always a date. once = ask when missing, then reuse "
                "the saved value; confirm = prefilled and re-confirmed each booking; expires = a date that "
                "lapses. Use gate ONLY when the owner says a lapsed or missing date must STOP the booking; when they say it should remind them, or say nothing about blocking, write no gate. A date that runs out is ALWAYS 'expires', gated or not: reminding is what 'expires N' does, so a date the owner wants to be reminded about is 'expires N' with no gate, never 'confirm'. \"expires N\" sets "
                "recall N days before expiry; use the owner's chosen interval"
            ),
            "ladder": (
                "REPEATABLE, one per ordered progression — \"Who | Label | Level 1, Level 2, Level 3… | "
                "approve, auto or off | Service = minimum level; …\". Who is a configured Kind, or customer "
                "when the booking is about the customer. Use the owner's levels in order. Notification is "
                "approve (draft for approval), auto or off. An optional service minimum is a HARD GATE: the "
                "diary refuses that service below the required level. This also represents a prerequisite step "
                "before another service can be booked. Propose it when the owner describes that sequence, "
                "using only their named steps, services and required levels; never invent a sequence"
            ),
        },
    },
    "crm_fields": {
        "label": "Customer fields",
        "href": "/app/crm/fields",
        "repeat": {"contact_field"},
        "fields": {
            "contact_field": (
                "REPEATABLE, one per thing to know about the CUSTOMER themselves (not a dependant) — \"Label | "
                "text, number, date, dropdown: option, option or yes/no | expires N valid M | hint\" (a "
                "dropdown MUST list its options after the colon — 'dropdown: Hereford, Worcester' — or it "
                "cannot be created; never a field for a progress level, stage, grade or belt — that is a "
                "LADDER on the dependants page and already lives on the customer record) (expires N = a date "
                "that lapses, recalled N days BEFORE it does — N is the recall, never how long the thing "
                "lasts: use the owner's stated recall interval; add valid M when the date on record is the day "
                "it was ISSUED and it runs M MONTHS from then — 'expires N valid M', replacing N and M with "
                "the owner's stated intervals; leave M out when the date on record is the expiry itself; leave "
                "the third part blank for a plain field). ONLY for facts about the person booking: a "
                "certificate they hold, a membership, a licence — never a job detail. When a BOOKING needs the "
                "fact, write it as a booking_details row with a remember part instead — that makes the same "
                "customer field AND the booking asks for it; a contact_field here is for a fact no booking "
                "needs."
            ),
        },
    },
    # Getting paid as a card (run log F6, services take 2026-09-25): the page
    # used to have none, so its arrival ask wrote an 18.9 s, 747-token essay
    # that also called the default terms "your notes". The facts the owner has
    # given — terms, the deposit and when it's asked, quote validity, VAT, the
    # footer — fill it like any other step; name, address, logo and bank
    # details stay theirs to type.
    "invoice_settings": {
        "label": "Getting paid",
        "href": "/app/invoices/settings",
        "repeat": set(),
        "fields": {
            "default_deposit_percent": (
                (
                    "the owner's stated deposit percentage as a number, with no percent sign; whether taken on "
                    "quote acceptance, booking confirmation or both"
                )
            ),
            # Kris's services take (2026-09-25, run log F4): "urgent visits are paid on
            # completion; a new install takes a 25% deposit when the quote's
            # accepted" held 25 and nothing else, and the page's "ask when a
            # booking is confirmed" defaults ON — so Sarah's £85 urgent visit would
            # have gone out with a £21.25 deposit invoice. Which of the two the
            # owner meant has to be held beside the percentage.
            "deposit_on_confirm": (
                "1 only when the owner says a deposit secures every confirmed booking; 0 when it is only on "
                "quote acceptance or only for some services. Supply with default_deposit_percent; ask if the "
                "trigger is unclear"
            ),
            "payment_terms": (
                "when and how customers pay, in the owner's words; preserve every stated distinction between "
                "services or customer groups"
            ),
            "vat_rate": "the owner's applicable VAT rate as a number; never infer a tax rate from location or registration alone",
            "show_vat": "'1' when the business is VAT registered and adds VAT to its prices; '0' when not",
            "prices_include_vat": (
                "'1' when the prices the owner states to customers already include VAT; '0' when VAT is added on"
                " top of them. Set it only from what the owner says about their prices; when VAT is on and they have not s"
                "aid, ask: when you tell a customer a price, is VAT already in it?"
            ),
            "default_quote_valid_days": "days a quote stays valid, number",
            "show_payment_terms": "'1' whenever you give payment_terms (prints them on the invoice)",
            "footer_note": (
                "a short invoice footer from the owner's own facts, such as their stated registration or "
                "reference; never invent an organisation or number"
            ),
        },
    },
    "crm_settings": {
        "label": "CRM settings",
        "href": "/app/crm/settings",
        "repeat": {"stage"},
        "fields": {
            "stage": (
                "REPEATABLE, one per pipeline stage in order — \"Name | type\" (type is "
                "active,won,lost,dormant). Use the CUSTOMER'S OWN WORDS from their brief and notes; do not "
                "substitute a different service name"
            ),
            "quiet_days": "days of silence before a contact is flagged gone-quiet, 1-365",
        },
    },
}

PAGE_KEYS = ("",) + tuple(PAGES)


# ── stored codes, said in the owner's words ──────────────────────────────────
# A card line and the "Kept for later" receipt both used to print the stored
# code ("Windows: ampm", "Working days: mon,tue,wed,thu,fri,sat",
# "Recurring enabled: 1", "Service out of hours: Name | evenings … | | yes |
# 140 | 65") — first-sign-up film, 30 Sep 2026, on camera. words() turns a
# known code into the words the owner would use; an unknown one returns None
# and the caller keeps the plain "name: value" form. No trade words here: the
# vocabulary is the settings', not any business's.
_DAY_WORDS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_FREQ_WORDS = {"weekly": "weekly", "fortnightly": "fortnightly", "monthly": "monthly",
               "six_weekly": "every six weeks", "quarterly": "quarterly"}
_NATION_WORDS = {"england-and-wales": "England and Wales", "scotland": "Scotland",
                 "northern-ireland": "Northern Ireland"}
_WINDOW_WORDS = {"ampm": "morning and afternoon windows", "slots": "timed slots",
                 "day": "whole-day bookings", "half": "a morning or afternoon window",
                 "time": "an exact time"}
_LABELS = {
    ("diary_settings", "open_time"): "Opens", ("diary_settings", "close_time"): "Closes",
    ("diary_settings", "working_days"): "Working days", ("diary_settings", "windows"): "Bookings",
    ("diary_settings", "recurring_enabled"): "Repeat bookings",
    ("diary_settings", "recurring_freq"): "Repeat bookings",
    ("diary_settings", "noun_booking"): "Your word for a booking",
    ("diary_settings", "noun_participant"): "Your word for who's booked in",
    ("diary_settings", "bank_holiday_nation"): "Bank holidays",
    ("diary_settings", "party_enabled"): "Bookings count people",
    ("diary_settings", "makeups_on"): "Make-up sessions",
    ("diary_settings", "service"): "Service",
    ("diary_settings", "service_urgent"): "Same-day",
    ("diary_settings", "service_out_of_hours"): "Out of hours",
    ("diary_settings", "service_after_job"): "After the job",
    ("dependants", "group"): "Kind",
    ("dependants", "field"): "Detail",
    ("dependants", "ladder"): "Progression",
    ("crm_settings", "stage"): "Stage",
    ("invoice_settings", "show_payment_terms"): "Payment terms on invoices",
    ("invoice_settings", "show_vat"): "VAT on invoices",
    ("invoice_settings", "prices_include_vat"): "Prices include VAT",
    ("invoice_settings", "deposit_on_confirm"): "Deposit on every confirmed booking",
    ("diary_settings", "stay_mode"): "Booking type",
}
_BOOLS = {("diary_settings", "recurring_enabled"), ("diary_settings", "party_enabled"),
          ("diary_settings", "makeups_on"), ("diary_settings", "stay_mode"),
          ("invoice_settings", "show_payment_terms"),
          ("invoice_settings", "show_vat"), ("invoice_settings", "deposit_on_confirm"),
          ("invoice_settings", "prices_include_vat")}
_NOUNS = {("diary_settings", "noun_booking"), ("diary_settings", "noun_participant")}


def _codes(value: str) -> list[str]:
    return [c.strip().lower() for c in str(value).split(",") if c.strip()]


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def days_words(value: str) -> str | None:
    """"mon,tue,wed,thu,fri,sat" → "Monday to Saturday"; a broken run lists the
    days; every day → "every day". None when a code is not a weekday."""
    codes = _codes(value)
    if not codes or any(c not in _DAY_WORDS for c in codes):
        return None
    order = [d for d in _DAYS if d in codes]
    if len(order) == 7:
        return "every day"
    idx = [_DAYS.index(d) for d in order]
    if len(order) >= 3 and idx == list(range(idx[0], idx[0] + len(idx))):
        return f"{_DAY_WORDS[order[0]]} to {_DAY_WORDS[order[-1]]}"
    return _join([_DAY_WORDS[d] for d in order])


def _money(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    return raw if raw[0] in "£$€" else f"£{raw}"


def words(page: str, name: str, value: str) -> str | None:
    """The value alone, in words ("Monday to Saturday", "yes", "monthly, every
    six weeks and quarterly"); None when the code is not one we know."""
    key = (page, name)
    v = str(value).strip()
    if key in _BOOLS:
        return {"1": "yes", "true": "yes", "0": "no", "false": "no"}.get(v.lower())
    if key == ("diary_settings", "stay_mode"):
        return {"1": "yes", "0": "no"}.get(v)
    if key in _NOUNS:
        return f"“{v}”" if v else None
    if key == ("diary_settings", "working_days"):
        return days_words(v)
    if key == ("diary_settings", "windows"):
        return _WINDOW_WORDS.get(v.lower())
    if key == ("diary_settings", "recurring_freq"):
        names = [_FREQ_WORDS.get(c) for c in _codes(v)]
        if not names or any(n is None for n in names):
            return None
        return _join(names)
    if key == ("diary_settings", "bank_holiday_nation"):
        n = _NATION_WORDS.get(v.lower())
        return f"closed on {n} bank holidays" if n else None
    if key == ("diary_settings", "service"):
        p = _parts(v)
        bits = [f"{p[1]} min" if len(p) > 1 and p[1] else "",
                _money(p[2]) if len(p) > 2 else "",
                _WINDOW_WORDS.get(p[3].lower(), p[3]) if len(p) > 3 and p[3] else ""]
        tail = ", ".join(b for b in bits if b)
        return f"{p[0]} — {tail}" if tail else p[0]
    if key == ("diary_settings", "service_out_of_hours"):
        # "Name | evenings until HH:MM | extra days | bank holidays yes/no |
        # first-hour price | price an hour after"
        p = _parts(v) + [""] * 6
        when = [x for x in (p[1], p[2]) if x]
        if p[3].lower() in ("yes", "1", "true") and not any("bank holiday" in x.lower() for x in when):
            when.append("bank holidays")
        price = ""
        if p[4]:
            price = f"{_money(p[4])} first hour"
            if p[5]:
                price += f", then {_money(p[5])} an hour"
        tail = ", ".join(x for x in (" and ".join(when), price) if x)
        return f"{p[0]} — {tail}" if tail else p[0]
    if key == ("diary_settings", "service_urgent"):
        # "Service name | cut-off HH:MM | notice hours"
        p = _parts(v) + [""] * 3
        bits = []
        if p[1]:
            bits.append(f"same day if asked before {p[1]}")
        if p[2] and p[2] != "0":
            bits.append(f"{p[2]} hour{'' if p[2] == '1' else 's'} notice")
        tail = ", ".join(bits)
        return f"{p[0]} — {tail}" if tail else p[0]
    if key == ("diary_settings", "service_after_job"):
        p = _parts(v)
        rest = ", ".join(x for x in p[1:] if x)
        return f"{p[0]} — {rest}" if rest else p[0]
    if key == ("dependants", "group"):
        # "Kind | plural | one or several"
        p = _parts(v) + [""] * 3
        many = p[2].lower()
        how = "one each" if many.startswith("one") else "any number each" if many else ""
        plural = f"(plural {p[1]})" if p[1] and p[1].lower() != p[0].lower() else ""
        tail = " ".join(x for x in (plural, how) if x)
        return f"{p[0]} {tail}".strip() if tail else p[0]
    if key == ("dependants", "field"):
        # "Kind | Label | type | required or optional | once, confirm or expires | gate or no gate | hint"
        p = _parts(v) + [""] * 7
        bits = []
        if p[3]:
            bits.append("required" if p[3].lower().startswith("req") else "optional")
        if p[2] and p[2].lower() != "text":
            bits.append("yes or no" if p[2].lower() == "yes/no" else f"a {p[2].lower()}")
        mem = p[4].lower()
        if mem.startswith("once"):
            bits.append("asked once and kept")
        elif mem.startswith("confirm"):
            bits.append("confirmed each visit")
        elif mem.startswith("expir"):
            bits.append("with an expiry date")
        if p[5].lower().startswith("gate"):
            bits.append("booking waits for it")
        tail = ", ".join(bits)
        head = f"{p[1]} ({p[0]})" if p[0] and p[1] else (p[1] or p[0])
        return f"{head} — {tail}" if tail else head
    if key == ("dependants", "ladder"):
        # "Who | Label | Level 1, Level 2, … | approve, auto or off | Service = minimum level; …"
        p = _parts(v) + [""] * 5
        bits = []
        if p[2]:
            bits.append(p[2])
        notify = p[3].lower()
        if notify.startswith("approve"):
            bits.append("moves drafted for your approval")
        elif notify.startswith("auto"):
            bits.append("moves sent automatically")
        elif notify.startswith("off"):
            bits.append("no move-up messages")
        if p[4]:
            bits.append(p[4])
        tail = "; ".join(bits)
        head = f"{p[1]} ({p[0]})" if p[0] and p[1] else (p[1] or p[0])
        return f"{head} — {tail}" if tail else head
    if key == ("crm_settings", "stage"):
        # "Name | type" (active, won, lost, dormant)
        p = _parts(v) + [""] * 2
        kind = {"active": "in progress", "won": "won", "lost": "lost",
                "dormant": "gone quiet"}.get(p[1].lower(), "")
        return f"{p[0]} ({kind})" if kind and kind != "in progress" else p[0]
    return None


def display(page: str, name: str, value: str) -> str | None:
    """One card line in words — "Working days: Monday to Saturday" — or None
    when the widget should print the plain "name: value" form."""
    w = words(page, name, value)
    if w is None:
        return None
    label = _LABELS.get((page, name)) or name.replace("_", " ").capitalize()
    return f"{label}: {w}"


def with_display(page: str, values: list[dict]) -> list[dict]:
    """The cleaned values plus a `display` line where one exists (the widget
    reads it; the apply path ignores it)."""
    out = []
    for v in values:
        from app import setup_rows
        if v.get("problems"):
            out.append({**v, "display": "Needs your answer: " + " ".join(v["problems"])})
            continue
        if v.get("fields"):
            d = setup_rows.describe(page, v["name"], v["fields"], v.get("provided"))
        else:
            d = display(page, v.get("name", ""), v.get("value", ""))
        out.append({**v, "display": d} if d else dict(v))
    return out


def catalogue_block(today: date | None = None) -> str:
    """The prompt section listing every recommendable page and its allowed field
    names — the model may only use these names, so a recommendation always maps
    onto the real form. Opens with today's date: run #7 (2026-09-05) had the
    card write half-term as 2025-10-27..30 — a year in the past — because the
    prompt carried no "today" and no rule for a date given without a year."""
    today = today or date.today()
    lines = [f"Today is {today.isoformat()} ({today.strftime('%A')}). A date the owner "
             "gives without a year is the NEXT such date from today; never recommend "
             "a blackout date that has already passed. A closure WITH a stated year "
             "that has already passed is not next year's closure — drop it and say "
             "so (run #10: '29 April–6 May 2026' was written as 2027); only a date "
             "given without a year rolls forward.",
             (
                 "A diary has ONE shape — pick it and say which in one line: STAYS (arrival-to-departure spans "
                 "against concurrent capacity: stay_rate + stay_rate_unit, same-day visits are stays with min "
                 "nights 0, their timings live in the knowledge notes), or SERVICES (timed, half-day or "
                 "whole-day appointments — service rows), or WINDOWS. Never fill service rows AND stay pricing "
                 "on one card — EXCEPT for APPOINTMENTS: on a STAYS diary a service row is a timed visit the "
                 "customer comes to (use the configured appointment service) — \"Name | minutes | price | time\" "
                 "— and each one ALSO gets a resource row of the SAME name whose 'how many' is how many can run "
                 "at once (\"Service name | capacity | | booking\", using the owner's stated capacity); those "
                 "pools are separate from the spaces. Never write a stay itself (an arrival-to-departure span) "
                 "as a service row."
             ),
             "Every field below is on the page named — a diary_settings card carries the "
             "Cancellations & credits knobs (credits_spend, late_cancel_hours, no_show_spends, "
             "excused_refunds, late_move_charged, hours_unit) itself whenever the brief states a cancellation rule or "
             "sells blocks; never write that they 'sit on the Packages page' or leave them to a "
             "later step — an unset late_cancel_hours silently keeps the 24-hour default."]
    for key, page in PAGES.items():
        lines.append(f"page \"{key}\" — {page['label']}:")
        for name, desc in page["fields"].items():
            lines.append(f"  - {name}: {desc}")
        if key == "diary_settings":
            lines.append("  - dayhours_<day>_open / dayhours_<day>_close: per-day "
                         "opening-hour override, HH:MM (day = mon..sun)")
    return "\n".join(lines)


def _valid_name(page: str, name: str) -> bool:
    # Read compatibility for old recommendations only. The current catalogue
    # exposes stay_mode under diary_settings, never booking_details.
    if page == "booking_details" and name == "stay_mode":
        return True
    if name in PAGES[page]["fields"]:
        return True
    return page == "diary_settings" and bool(_DAYHOURS_RE.match(name))


def next_dates(value: str, today: date | None = None) -> str:
    """Blackout dates as the model wrote them, each rolled forward to the next
    occurrence of that day-month if it has already passed (run #7: the model
    dated a half-term to last year). Unparseable lines are dropped; the result
    keeps the one-per-line form the diary form expects."""
    today = today or date.today()
    out: list[str] = []
    for raw in re.split(r"[\n,;]+", str(value or "")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if d < today:
            try:
                d = d.replace(year=today.year)
            except ValueError:            # 29 Feb
                d = d.replace(year=today.year, day=28)
            if d < today:
                d = d.replace(year=today.year + 1)
        if d.isoformat() not in out:
            out.append(d.isoformat())
    return "\n".join(out)


_DEBRIS_TAIL = re.compile(r"[\s\]\}\[\{,\"']+$")


def _strip_debris(value: str) -> str:
    """Remove trailing JSON bracket debris ("... | only for: |}]},"). The tail must
    contain a closing bracket or brace to count: a value that legitimately ends
    in a quote or a comma is left alone (first-walk re-run, 1 Oct 2026)."""
    m = _DEBRIS_TAIL.search(value)
    if m and ("]" in m.group() or "}" in m.group()):
        return value[:m.start()].strip()
    return value


def clean(page, values, today: date | None = None) -> list[dict] | None:
    """Validate one model recommendation: known page, whitelisted names, trimmed
    capped values, non-repeatable names deduped (first wins). Returns the cleaned
    [{name, value}] list, or None when nothing valid remains (no card shown)."""
    page = (page or "").strip()
    if page not in PAGES:
        return None
    repeat = PAGES[page]["repeat"]
    seen: set = set()
    out: list[dict] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = _strip_debris(str(item.get("value") or "").strip())
        if not name or not value or not _valid_name(page, name):
            continue
        # No silent cut: a cut row ends in a name that does not exist (first-walk
        # re-run, 1 Oct 2026). A value over its limit is rejected whole.
        limit = PROSE_LIMITS.get((page, name)) or (MAX_ROW if name in repeat else MAX_VALUE)
        if len(value) > limit:
            log.warning("settings_recommend: dropped over-long value page=%s field=%s "
                        "(%d chars, limit %d)", page, name, len(value), limit)
            continue
        if name == "blackout_dates":
            value = next_dates(value, today=today)
            if not value:
                continue
        if name not in repeat:
            if name in seen:
                continue
            seen.add(name)
        else:
            # Repeatable entries dedupe on their label — the first "|"-part
            # (live e2e 2026-09-02: the model listed "Departure / collection
            # date & time" twice and the checklist gained a duplicate row).
            n_parts = PAGES[page].get("key_parts", {}).get(name, 1)
            head = [x.strip().lower() for x in value.split("|")[:n_parts]]
            label_key = (name, "|".join(head))
            if label_key in seen:
                continue
            seen.add(label_key)
        out.append({"name": name, "value": value})
        if len(out) >= MAX_ENTRIES:
            break
    if page == "diary_settings":
        out = _hours_unit_by_arithmetic(out)
    if page == "crm_fields":
        out = _recall_is_not_a_validity(out)
    if page == "booking_details":
        out = _remember_part_is_canonical(_stay_mode_owns_the_collection_time(out))
    return out or None


_REMEMBER_PART = re.compile(r"^\s*remember(?:ed)?\s*[:=\-–—]?\s*(.*)$", re.IGNORECASE)


def _remember_part_is_canonical(values: list[dict]) -> list[dict]:
    """A field row's remember part (app/intake_memory) sits wherever the model
    put it — third, fourth, or in place of the 'only for' part it left out. The
    card's script reads it by POSITION (the fifth part), so it is moved there:
    "Label | required | hint | only for: … | remember: once". A part that reads
    as ask-every-time ('remember: each visit') is dropped — that is the default
    and needs no part. Unknown words drop the part rather than guess."""
    from app import intake
    for v in values:
        if v.get("name") != "field":
            continue
        parts = [x.strip() for x in str(v.get("value") or "").split("|")]
        mem, rest = "", []
        for i, p in enumerate(parts):
            m = _REMEMBER_PART.match(p) if i > 0 else None
            if m:
                mem = intake.clean_remember(m.group(1))
            else:
                rest.append(p)
        if not mem or mem == "ask":
            if len(rest) != len(parts):
                v["value"] = " | ".join(rest).rstrip(" |")
            continue
        while len(rest) < 4:
            rest.append("")
        v["value"] = " | ".join(rest[:4] + [f"remember: {mem}"])
    return values


# A stays diary's reserved departure/collection field IS the collection time
# (the Overnight-stays toggle adds it). A `field` row asking for it again made
# the agent ask the customer twice (run #10 (aaa)) — dropped when stay_mode is
# on, whatever the model wrote.
_COLLECTION_WORDS = ("collection", "collect", "departure", "depart", "pick-up", "pick up",
                     "pickup", "drop-off and collection", "return time")


def _stay_mode_owns_the_collection_time(values: list[dict]) -> list[dict]:
    if not any(v["name"] == "stay_mode" and v["value"].strip() in ("1", "true", "yes")
               for v in values):
        return values
    out = []
    for v in values:
        if v["name"] == "field":
            label = v["value"].split("|")[0].strip().lower()
            if any(w in label for w in _COLLECTION_WORDS):
                continue
        out.append(v)
    return out


_EXPIRES_RE = re.compile(r"expires?\s*(\d+)(?!\s*valid)", re.IGNORECASE)


def _recall_is_not_a_validity(values: list[dict]) -> list[dict]:
    """"expires 730" for a two-year certificate (run #8): a recall cannot exceed
    a year, so a number past 365 is how long the thing LASTS — rewritten to
    the default recall plus "valid <months>" so the field means what the
    owner meant; a small N stays the recall it is."""
    for v in values:
        if v.get("name") != "contact_field":
            continue
        parts = [x.strip() for x in str(v.get("value") or "").split("|")]
        if len(parts) < 3 or re.search(r"valid\s*\d+", parts[2], re.IGNORECASE):
            continue
        m = _EXPIRES_RE.search(parts[2])
        if not m or int(m.group(1)) <= 365:
            continue
        months = max(1, round(int(m.group(1)) / 30.4))
        parts[2] = f"expires 60 valid {months}"
        v["value"] = " | ".join(parts)
    return values


def _hours_unit_by_arithmetic(values: list[dict]) -> list[dict]:
    """A package sold in hours spends by the minute unless every service is
    exactly one hour: under `hour` a 2-hour session spends 1.0 (wallet.
    spend_amount). Run #8 (2026-09-05): two fresh cards for a school with 60-
    and 120-minute sessions wrote `hour` however the catalogue phrased it —
    the arithmetic decides, not the wording."""
    minutes = []
    for v in values:
        if v.get("name") != "service":
            continue
        parts = [x.strip() for x in str(v.get("value") or "").split("|")]
        try:
            minutes.append(int(float(parts[1])))
        except (IndexError, ValueError):
            continue
    if minutes and any(m != 60 for m in minutes):
        for v in values:
            if v.get("name") == "hours_unit" and str(v.get("value") or "").strip().lower() != "minutes":
                v["value"] = "minutes"
    return values


def _parts(value: str) -> list[str]:
    return [p.strip() for p in str(value or "").split("|")]


def parse_stages(values) -> list[dict]:
    """The CRM apply path's parse of repeatable \"Name | type\" stage entries —
    bad types default to 'active', blanks and duplicate names (case-insensitive)
    are dropped. Order follows the list."""
    out, seen = [], set()
    for item in values or []:
        if (item or {}).get("name") != "stage":
            continue
        parts = _parts(item.get("value"))
        name = parts[0][:60] if parts else ""
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        stage_type = parts[1].lower() if len(parts) > 1 else "active"
        if stage_type not in ("active", "won", "lost", "dormant"):
            stage_type = "active"
        out.append({"name": name, "stage_type": stage_type})
    return out


def parse_quiet_days(values) -> int | None:
    """The recommended gone-quiet threshold, clamped 1-365, or None."""
    for item in values or []:
        if (item or {}).get("name") == "quiet_days":
            try:
                return max(1, min(int(str(item.get("value")).strip()), 365))
            except (TypeError, ValueError):
                return None
    return None
