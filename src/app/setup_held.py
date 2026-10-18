"""Held for later — facts the owner gives at one setup step that belong to
another page, kept against the tenant and rendered into that page's form
when they reach it.

Kris, stays run #10 (2026-09-13): "info for a future step has been
presented & picked up on, it should be held until we reach that step, then
it's the first thing in there waiting… could just be prefilled." The
LittleDogs visit lost capacity 30 / notice 10 h / deposit 25% because the
chat that heard them had nowhere to keep them (per-browser thread; the
phone died). Every assistant turn — the wizard co-pilot and the in-app
help — may now return `held`: a list of {page, values} in the SAME shape
as a settings recommendation (src/app/settings_recommend.py), so a held fact
is exactly a card value captured early. This module is the contract:

  * PAGES — the recommendable catalogue plus two pages the catalogue does
    not cover (the wizard's Voice step; Invoice details), with their field
    whitelists;
  * clean_many() — validate what the model returned (only real pages, only
    real field names, trimmed, capped);
  * add() / load() / mark_used() — the per-tenant record in
    tenant_config.setup_held (jsonb, applied 2026-09-13): the owner's LATEST
    statement wins for a single-value field, repeatable rows accumulate;
  * for_page() — the unused items a page renders into its form (prefill
    fills EMPTY fields only — an owner's saved value is never overwritten);
  * prompt_block() — the facts, page by page, for the prompts: use them
    verbatim, never guess against them, never ask for them again.

Nothing here applies a setting: a page's own Save stays the only write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import db
from app import settings_recommend as sr

log = logging.getLogger("blautom8.setup_held")

MAX_ITEMS = 120
MAX_VALUE = sr.MAX_VALUE

# Pages the recommend catalogue does not carry but an owner talks about early.
EXTRA_PAGES: dict[str, dict] = {
    "voice": {
        "label": "Voice",
        "href": "/onboarding/voice",
        "repeat": set(),
        "fields": {
            "agent_name": "what the agent calls itself",
            "tone": "tone of voice, in the owner's words",
            "sign_off": "the email sign-off, exactly as given",
        },
    },
    # Getting paid moved into the recommend catalogue (settings_recommend,
    # run log F6) — its held facts ride the same whitelist as its card.
}


def pages() -> dict[str, dict]:
    """Every page a held fact may name: the recommend catalogue + the extras."""
    out = dict(sr.PAGES)
    out.update(EXTRA_PAGES)
    return out


def page_keys() -> list[str]:
    return list(pages())


def label(page: str) -> str:
    spec = pages().get(page) or {}
    return spec.get("label") or page


def catalogue_extra_block() -> str:
    """The extras, in the catalogue's own format, appended after
    settings_recommend.catalogue_block() in the prompts."""
    lines = []
    for key, page in EXTRA_PAGES.items():
        lines.append(f"page \"{key}\" — {page['label']}:")
        for name, desc in page["fields"].items():
            lines.append(f"  - {name}: {desc}")
    return "\n".join(lines)


def clean(page: str, values) -> list[dict]:
    """One page's values, validated: the catalogue pages go through
    settings_recommend.clean (whitelist, trim, cap, dedupe, date fixes); the
    extras through their own whitelist. [] when nothing survives."""
    if page in sr.PAGES:
        return sr.clean(page, values) or []
    spec = EXTRA_PAGES.get(page)
    if not spec:
        return []
    out, seen = [], set()
    for v in values or []:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()
        value = " ".join(str(v.get("value") or "").split())
        if name not in spec["fields"] or not value or name in seen:
            continue
        limit = 400 if name in ("tone",) else MAX_VALUE     # a tone is a sentence or two
        if len(value) > limit:
            log.warning("setup_held: dropped over-long value page=%s field=%s "
                        "(%d chars, limit %d)", page, name, len(value), limit)
            continue
        seen.add(name)
        out.append({"name": name, "value": value})
    return out


def clean_many(held) -> list[dict]:
    """The model's `held` list → [{page, values}] with only real pages and
    real fields; empty entries dropped."""
    out = []
    for entry in held or []:
        if not isinstance(entry, dict):
            continue
        page = str(entry.get("page") or "").strip()
        if page not in pages():
            continue
        values = clean(page, entry.get("values"))
        if values:
            out.append({"page": page, "values": values})
    return out


def _is_repeat(page: str, name: str) -> bool:
    return name in (pages().get(page) or {}).get("repeat", set())


def merge(existing: list[dict], page: str, values: list[dict], step: str) -> list[dict]:
    """Fold one page's values into the record. A single-value field keeps
    only the owner's LATEST statement (a correction replaces); a repeatable
    row is added unless an identical row already exists. Used items are
    never revived — a re-stated fact after Save is a new, unused item."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = [dict(i) for i in (existing or []) if isinstance(i, dict)]
    for v in values:
        name, value = v["name"], v["value"]
        if _is_repeat(page, name):
            if any(i.get("page") == page and i.get("name") == name and i.get("value") == value
                   and not i.get("used_at") for i in items):
                continue
        else:
            items = [i for i in items
                     if not (i.get("page") == page and i.get("name") == name and not i.get("used_at"))]
        items.append({"page": page, "name": name, "value": value, "step": step, "at": now})
    return items[-MAX_ITEMS:]


def load(tenant_id: str, client=None) -> list[dict]:
    """The tenant's record — [] on any failure (a read blip never breaks a turn)."""
    try:
        row = db.get_tenant_config(tenant_id, client=client) or {}
        items = row.get("setup_held") or []
        return [i for i in items if isinstance(i, dict)]
    except Exception:
        log.warning("setup_held load failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def save(tenant_id: str, items: list[dict], client=None) -> None:
    db.upsert_tenant_config(tenant_id, {"setup_held": items}, client=client)


def describe(page: str, name: str, value: str) -> str:
    """One held value as a few words for the receipt line: a repeatable row
    by its own label ("Certificates (Subject)", "Subject"), a scalar as "capacity
    per window 30"."""
    parts = [p.strip() for p in str(value).split("|")]
    if _is_repeat(page, name):
        if name == "field" and page == "dependants" and len(parts) > 1:
            return f"{parts[1]} ({parts[0]})"
        if name == "note" and parts:
            return parts[0]
        head = parts[0] if parts and parts[0] else name
        # A diary row says what kind of row it is ("service Emergency"), not a
        # bare word (first-sign-up film, 30 Sep 2026: "Diary settings: Emergency").
        if page == "diary_settings":
            return f"{_ROW_KINDS.get(name, name.replace('_', ' '))} {head}"
        return head
    pretty = name.replace("_", " ")
    # A setting's stored code is not what the owner said (onboarding film,
    # 30 Sep 2026: "Diary settings: windows ampm" on screen). Known codes read
    # as words ("working days Monday to Saturday", "repeat bookings yes");
    # anything else keeps the plain "name value" form. One vocabulary, shared
    # with the recommendation card: settings_recommend.words().
    said = sr.words(page, name, value)
    if said is not None:
        if (page, name) in _BARE_WORDS:
            return said
        # A switch reads by its label ("payment terms on invoices yes"), not
        # its column name ("show payment terms yes").
        label = sr._LABELS.get((page, name)) if (page, name) in sr._BOOLS else None
        return f"{(label or pretty).lower()} {said}"
    return f"{pretty} {value}" if len(str(value)) <= 24 else pretty


# Receipt words that stand on their own without the setting's name in front.
_BARE_WORDS = {("diary_settings", "windows"), ("diary_settings", "bank_holiday_nation")}
_ROW_KINDS = {"service": "service", "resource": "resource", "service_urgent": "same-day service",
              "service_out_of_hours": "out of hours", "service_after_job": "after the job"}


# The wizard's order — a held page that sits at or before the current step is
# already done and is dropped (re-run 2026-09-13: on Knowledge the model held
# the Voice values again from the thread and the panel said "Kept for later —
# Voice" for a step already behind the owner). The in-app help turns
# ("help") are past the whole wizard.
WIZARD_ORDER = ("interview", "mailbox", "voice", "knowledge")
# held page → the wizard step that owns it. Knowledge counts too (Kris,
# re-run: "we've passed knowledge") — on the Knowledge step a fact is a
# note in that reply, and in the app it is a note card offered right there.
WIZARD_PAGES = {"voice": "voice", "knowledge": "knowledge"}


def behind(page: str, step: str) -> bool:
    """True when `page` belongs to a wizard step at or before `step`."""
    owner = WIZARD_PAGES.get(page)
    if not owner:
        return False
    if step not in WIZARD_ORDER:
        return True                                  # in-app: the wizard is done
    return WIZARD_ORDER.index(owner) <= WIZARD_ORDER.index(step)


def _drop_saved_notes(tenant_id: str, entries: list[dict], client=None) -> list[dict]:
    """A knowledge note whose label already names a saved source is not new
    (re-run 2026-09-13: on the diary step the model held Prices — Day visits
    and three more that were saved minutes earlier). Best-effort: a read
    failure keeps the entries."""
    if not any(e["page"] == "knowledge" for e in entries):
        return entries
    try:
        saved = {str(s.get("source") or "").strip().lower()
                 for s in db.list_knowledge_sources(tenant_id, client=client)}
    except Exception:
        return entries
    out = []
    for e in entries:
        if e["page"] != "knowledge":
            out.append(e)
            continue
        keep = [v for v in e["values"]
                if v["name"] != "note"
                or str(v["value"]).split("|")[0].strip().lower() not in saved]
        if keep:
            out.append({"page": "knowledge", "values": keep})
    return out


def pages_for_path(path: str | None) -> set[str]:
    """The held pages whose form lives at `path` (query and fragment
    ignored; the invoice page carries both invoice_settings and packages) —
    so a turn never holds values for the page the owner is on."""
    path = (path or "").split("?")[0].split("#")[0]
    if not path:
        return set()
    return {key for key, spec in pages().items()
            if (spec.get("href") or "").split("#")[0] == path}


def add(tenant_id: str, held: list[dict] | None, step: str, client=None,
        on_pages: set[str] | None = None) -> list[str]:
    """Record what this turn held. Returns one receipt per page touched —
    "Dependants: Certificates (Subject), Subject" — and the widget's "Kept for
    later" line is only ever written from this return value, so it is never
    a claim the server did not make good on. Names what was kept (Kris,
    re-run 2026-09-13: two identical "Dependants" lines read as a repeat
    when the second turn had held something new). Best-effort: [] on
    failure."""
    cleaned = [e for e in clean_many(held) if not behind(e["page"], step)]
    # The page they are ON is never "later" (re-run 2026-09-13: "Kept for
    # later — Invoice details" on the Invoice details page, values already
    # in the form in front of them).
    if on_pages:
        cleaned = [e for e in cleaned if e["page"] not in on_pages]
    cleaned = _drop_saved_notes(tenant_id, cleaned, client=client)
    if not cleaned:
        return []
    try:
        items = load(tenant_id, client=client)
        receipts: list[str] = []
        for entry in cleaned:
            items = merge(items, entry["page"], entry["values"], step)
            what = ", ".join(describe(entry["page"], v["name"], v["value"]) for v in entry["values"][:4])
            more = len(entry["values"]) - 4
            if more > 0:
                what += f" +{more} more"
            receipts.append(f"{label(entry['page'])}: {what}")
        save(tenant_id, items, client=client)
        return receipts
    except Exception:
        log.warning("setup_held add failed (tenant=%s, step=%s)", tenant_id, step, exc_info=True)
        return []


def for_page(items: list[dict], page: str) -> list[dict]:
    """The unused values held for one page, as {name, value} pairs — the
    shape the page's prefill and the recommend card both speak."""
    return [{"name": i["name"], "value": i["value"]}
            for i in (items or [])
            if i.get("page") == page and not i.get("used_at") and i.get("name") and i.get("value")]


def mark_used(tenant_id: str, page: str, client=None) -> int:
    """The page was saved: its held items are done (kept, stamped, so a
    later re-statement reads as new). Returns how many were stamped."""
    try:
        items = load(tenant_id, client=client)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n = 0
        for i in items:
            if i.get("page") == page and not i.get("used_at"):
                i["used_at"] = now
                n += 1
        if n:
            save(tenant_id, items, client=client)
        return n
    except Exception:
        log.warning("setup_held mark_used failed (tenant=%s, page=%s)", tenant_id, page, exc_info=True)
        return 0


def prompt_block(items: list[dict], exclude_page: str | None = None) -> str:
    """The facts the owner has already given, page by page, for the prompts.
    Empty string when there is nothing unused."""
    by_page: dict[str, list[str]] = {}
    for i in items or []:
        if i.get("used_at") or not i.get("name"):
            continue
        if exclude_page and i.get("page") == exclude_page:
            continue
        by_page.setdefault(i["page"], []).append(f"{i['name']} = {i['value']}")
    if not by_page:
        return ""
    lines = ["FACTS THE OWNER HAS ALREADY GIVEN, held for the page they belong to. When "
             "you recommend or fill that page, use these VERBATIM — never guess a "
             "different number, never ask for them again, and never overwrite one with "
             "your own estimate:"]
    for page, vals in by_page.items():
        lines.append(f"- page \"{page}\" ({label(page)}): " + "; ".join(vals))
    return "\n".join(lines)


def schema() -> dict:
    """The `held` member of a structured reply: [{page, values:[{name, value}]}].
    Plain shapes only — the structured-output endpoint rejects validation
    keywords (minItems and friends)."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "enum": page_keys()},
                "values": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                        "required": ["name", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["page", "values"],
            "additionalProperties": False,
        },
    }


RULE = (
    (
        # Run log F3 (services take 2026-09-25): "how a first booking works" was
        # saved as a note and nothing else — the diary, the checklist and the
        # terms each had to be told again later.
        "HELD FOR LATER: setup has more pages than this one — the diary (hours, capacity, notice, "
        "prices, services, stays, blackout dates), booking details, dependants (the separately tracked "
        "subjects of bookings), customer fields, the CRM pipeline, invoice details (deposit %, VAT, "
        "payment terms), the voice step and the knowledge base. When the customer states a fact that "
        "belongs to one of THOSE pages rather than to this step, put it in `held` under that page "
        "using the exact field name from the catalogue (repeatable rows in their pipe format), and say "
        "in one short clause that it is kept for that step ('I have kept that for your diary "
        "settings'). A fact for THIS step goes in this step's own fields, never in `held`. Never hold "
        "a fact for a step already behind the customer — once past the Voice step nothing is held for "
        "it, and a fact they gave at an earlier step is not new. Knowledge notes are held only BEFORE "
        "the Knowledge step: on it they go in `notes`, and after it (inside the app) a new fact goes "
        "on a `knowledge` recommend card in the same reply, saved with a click — never in `held`. "
        "Never hold a knowledge note that is already saved. Never invent a value to hold. Leave `held` "
        "empty when nothing applies. A knowledge answer is often ALSO a setting: save the note AND "
        "hold the setting it fixes, each on its own page. Map required booking details to "
        "booking_details field rows; a named service to a diary_settings service row using only its "
        "stated duration, price and timing; payment terms to invoice_settings payment_terms in their "
        "words; a stated deposit percentage to default_deposit_percent together with whether it "
        "applies on confirmation (deposit_on_confirm) or only on quote acceptance. Ask for any missing "
        "value instead of supplying an example value. Only what they said — never a setting they "
        "didn't state."
    )
)
