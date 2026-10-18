"""CRM intelligence (phase 3 — docs/architecture/CRM_design.md §5).

One small Haiku call per email turns the mail flow into pipeline state:

- **Deal clustering** (inbound, on-write per §14): does this email belong to
  one of the contact's open deals, start a new one, or neither? A new deal
  lands in the tenant's first active stage with the email's activity stamped
  onto it — the pipeline page fills itself in, like everything else here.
- **Promise tracking** (both directions): commitments read out of the text —
  what WE owe them (mostly from our outgoing replies) and what THEY owe us.
  The killer view renders open ones; a click resolves them.

Everything is best-effort behind the pipeline's try/except: intelligence must
never cost a tenant their draft. Deduped end to end (crm_promises_source_uniq;
deal creation is once-per-email by construction — the analysis runs only when
the email_log row is genuinely new).
"""
import json
import logging
import re
from datetime import datetime, timezone

from app import agent, crm, crm_fields, db, llm, usage
from app.config import settings

log = logging.getLogger(__name__)

MIN_DEAL_CONFIDENCE = 0.6   # below this a "new deal" call is noise — skip it
MIN_SENTIMENT_CONFIDENCE = 0.5  # below this a mood read is a guess — keep the last one

# Sentiment (docs/architecture/CRM_design.md addendum): the customer's emotional temperature,
# read from every INBOUND email on the same Haiku call as deals/promises. The five
# moods are ordered — trend is the rank moving up (worsening) or down (improving)
# against what's stored on the contact. Outbound calls never carry the branch:
# our own reply's tone says nothing about how the CUSTOMER feels.
MOOD_RANK = {"positive": 0, "neutral": 1, "concerned": 2, "frustrated": 3, "angry": 4}


def mood_temperature(mood: str | None) -> str:
    """The UI's three-band read of a mood: calm / warm / hot."""
    if mood in ("frustrated", "angry"):
        return "hot"
    if mood == "concerned":
        return "warm"
    return "calm"

# ── The OS extension seam (docs/crm-vs-os-architecture.md §2.3, §3 Hygiene) ───
# R2: an OS depends on the CRM engine, never the reverse — so this module imports
# no OS module. An OS that wants to extend the same intel call
# registers here at import time instead. Each extension is
# gated per tenant by wants(); every non-participating tenant's analyze() stays
# unchanged (including inbound sentiment and the outbound 700-token cap).
_INTEL_EXTENSIONS: dict[str, dict] = {}


def register_intel_extension(key: str, schema: dict, system_prompt: str, wants,
                             apply_deal_hook=None, promise_resolved_hook=None) -> None:
    """Register an OS branch onto the intel call. `key` is the branch's property
    name in the analyze schema/output. `wants(tenant_id)`
    gates it per tenant — consulted on INBOUND calls only, like the capture itself.
    `apply_deal_hook(tenant_id, deal_id, branch, *, email_log_id)` applies the
    branch once the email has a deal; `promise_resolved_hook(tenant_id, promise_id)`
    fires when a they_owe promise auto-resolves. Hooks must be best-effort (never
    raise on the drafting hot path). Re-registering a key replaces it, so a module
    re-import stays idempotent."""
    if key in ("deal", "promises", "sentiment"):
        raise ValueError(f"intel extension key {key!r} collides with a base branch")
    _INTEL_EXTENSIONS[key] = {
        "key": key, "schema": schema, "system_prompt": system_prompt,
        "wants": wants, "apply_deal_hook": apply_deal_hook,
        "promise_resolved_hook": promise_resolved_hook,
    }

SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string",
                 "enum": ["positive", "neutral", "concerned", "frustrated", "angry"]},
        "trigger": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["mood", "trigger", "confidence"],
    "additionalProperties": False,
}

SENTIMENT_SYSTEM = (
    "\n3) sentiment: the CUSTOMER'S emotional temperature in THIS email. mood is "
    "one of positive (pleased, thanking, enthusiastic), neutral (routine business "
    "tone), concerned (worried, uncertain, needs reassurance), frustrated (chasing, "
    "impatient, disappointed — e.g. a repeat chase, 'still waiting', 'this is the "
    "third time'), angry (openly cross, threatening to leave, demanding escalation). "
    "trigger is a short phrase naming what earned the read, quoting or closely "
    "paraphrasing their words ('third chase about the invoice', '' for "
    "positive/neutral). confidence is your certainty 0-1. Judge only the customer's "
    "words — a terse but ordinary email is neutral, not angry. British understatement "
    "counts: 'a little disappointed' or 'I must say I expected better' reads as "
    "frustrated."
)

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "deal": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["none", "existing", "new"]},
                "deal_id": {"type": "string"},
                "title": {"type": "string"},
                "value_estimate": {"type": "number"},
                "confidence": {"type": "number"},
            },
            "required": ["action", "deal_id", "title", "value_estimate", "confidence"],
            "additionalProperties": False,
        },
        "promises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "reinforce", "resolve"]},
                    "who": {"type": "string", "enum": ["us", "them"]},
                    "description": {"type": "string"},
                    "due_hint": {"type": "string"},
                    "existing_promise_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "resolution_evidence": {"type": "string"},
                },
                "required": ["action", "who", "description", "due_hint",
                             "existing_promise_id", "confidence", "resolution_evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["deal", "promises"],
    "additionalProperties": False,
}

# A "promise" that is only the diary doing its job: keeping offered or held
# slots for the customer until they pick (run log F20).
_DIARY_HOLDS_IT = re.compile(
    r"\b(?:hold|holding|held|keep|keeping|reserve|reserving|reserved)\b[^.]{0,80}?"
    r"\b(?:slots?|spaces?|dates?|windows?|mornings?|afternoons?|times?|places?|bookings?|appointments?)\b",
    re.IGNORECASE)

SYSTEM = (
    "You read one business email and report two things as JSON.\n"
    "1) deal: is this email about one of the OPEN DEALS listed (action=existing "
    "+ its deal_id), the start of a genuinely NEW sales opportunity (action=new "
    "+ a short title in the seller's terms, value_estimate in GBP if stated, "
    "else 0), or neither (action=none — routine admin, invoices, thanks, "
    "support on delivered work)? confidence is your certainty in that call. "
    "Use deal_id='' and title='' when not applicable.\n"
    "2) promises: concrete commitments in THIS email. For each:\n"
    "- action='create': a genuinely NEW commitment not in the existing promises list.\n"
    "- action='reinforce': restates/confirms an existing open promise (set "
    "existing_promise_id to match).\n"
    "- action='resolve': this email FULFILS an existing promise (e.g. sends the "
    "quote that was promised, provides info that was owed). Set existing_promise_id "
    "and resolution_evidence (brief phrase explaining how it was fulfilled).\n"
    "who='us' when our side owes/fulfils it, who='them' when the customer does. "
    "description is one short sentence; due_hint is the deadline phrase verbatim "
    "('' if none). confidence is certainty of the action (0-1). "
    "existing_promise_id='' and resolution_evidence='' when not applicable. "
    "Only real commitments — no pleasantries, no maybes. NOT a promise: a booking this "
    "email places or confirms (the diary holds it — 'we\'ve booked Biscuit in for the "
    "7th' is not 'we owe: host Biscuit'); a deposit or invoice this email raises (the "
    "invoice holds it); slots or dates this email OFFERS or HOLDS for the customer to "
    "pick from (the diary keeps them until the deadline — 'we're holding Saturday "
    "morning for you until 8pm' is not 'we owe: hold the slots'; the customer's "
    "reply-by IS theirs: who='them', 'reply to pick a slot', due the deadline); "
    "a thing this email already does or attaches ('card attached' "
    "is done, not owed). What the customer must bring or do ON THE DAY (bring the food "
    "portioned, drop off at 8.30, meds labelled) IS worth keeping, as who='them' — say "
    "it as the thing to bring or do. A request the other side "
    "makes that THIS email refuses or redirects ('just add extra chlorine' → 'no, "
    "the tub comes out of use') is NOT a commitment on either side — do not create "
    "a promise for the refused thing, only for what was actually undertaken instead."
)


def build_prompt(*, direction: str, subject: str | None, body: str | None,
                 contact_name: str | None, company_name: str | None,
                 open_deals: list[dict], open_promises: list[dict] | None = None) -> str:
    lines = [f"This is an {'email FROM the customer' if direction == 'inbound' else 'reply WE sent to the customer'}.",
             f"Customer: {contact_name or 'unknown'}"
             + (f" at {company_name}" if company_name else "")]
    if direction == "outbound":
        lines.append("For promises: commitments in this reply are who='us'.")
    lines.append("Open deals with this customer:")
    if open_deals:
        for d in open_deals:
            stage = (d.get("crm_stages") or {}).get("name") or ""
            lines.append(f"- deal_id={d['id']}: {d.get('title') or 'Untitled'}"
                         + (f" ({stage})" if stage else ""))
    else:
        lines.append("- (none)")
    # Phase 3.5 §18.3/18.4: existing promises for dedup + resolution detection
    lines.append("\nExisting open promises with this customer:")
    if open_promises:
        for p in open_promises:
            side = "we owe" if p.get("direction") == "we_owe" else "they owe"
            due = f" (due: {p['due_hint']})" if p.get("due_hint") else ""
            lines.append(f"- promise_id={p['id']}: {side} — {p.get('description')}{due}")
    else:
        lines.append("- (none)")
    lines.append(f"\nSubject: {subject or '(none)'}")
    lines.append(f"Body:\n{agent.strip_quoted(body or '')[:4000]}")
    return "\n".join(lines)


def parse_analysis(text: str) -> dict:
    ext_keys = tuple(_INTEL_EXTENSIONS)
    try:
        data = json.loads(text)
        deal = data.get("deal") or {}
        promises = [p for p in (data.get("promises") or []) if isinstance(p, dict)]
        sentiment = data.get("sentiment") or {}
        out = {"deal": deal if isinstance(deal, dict) else {}, "promises": promises,
               "sentiment": sentiment if isinstance(sentiment, dict) else {}}
        for key in ext_keys:   # each registered OS branch, {} when absent
            branch = data.get(key) or {}
            out[key] = branch if isinstance(branch, dict) else {}
        return out
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"deal": {}, "promises": [], "sentiment": {},
                **{key: {} for key in ext_keys}}


def _analyze_schema(extensions: tuple = (), sentiment_mode: bool = False) -> dict:
    """The analyze schema — with a branch per ACTIVE registered OS extension
    and an inbound-only `sentiment` branch. A call with neither keeps the
    shipped base schema."""
    if not extensions and not sentiment_mode:
        return ANALYZE_SCHEMA
    props = dict(ANALYZE_SCHEMA["properties"])
    if sentiment_mode:
        props["sentiment"] = SENTIMENT_SCHEMA
    for ext in extensions:
        props[ext["key"]] = ext["schema"]
    return {**ANALYZE_SCHEMA, "properties": props}


async def analyze(tenant_id: str, *, direction: str, subject: str | None,
                  body: str | None, contact_name: str | None,
                  company_name: str | None, open_deals: list[dict],
                  open_promises: list[dict] | None = None,
                  email_log_id: str | None, extensions: tuple = ()) -> dict:
    # Sentiment rides every INBOUND call (all tenants); outbound stays byte-identical.
    sentiment_mode = direction == "inbound"
    system = SYSTEM + (SENTIMENT_SYSTEM if sentiment_mode else "") \
        + "".join(ext["system_prompt"] for ext in extensions)
    msg = await llm.create(
        client=agent.client(), tenant_id=tenant_id, operation="crm_intel",
        model=settings.anthropic_model,
        max_tokens=1000 if extensions else (800 if sentiment_mode else 700),
        system=system,
        messages=[{"role": "user", "content": build_prompt(
            direction=direction, subject=subject, body=body,
            contact_name=contact_name, company_name=company_name,
            open_deals=open_deals, open_promises=open_promises or [])}],
        output_config={"format": {"type": "json_schema",
                                  "schema": _analyze_schema(extensions, sentiment_mode)}},
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="crm_intel",
                           email_log_id=email_log_id)
    return parse_analysis(text)


def apply_deal(tenant_id: str, deal: dict, *, contact_id: str, company_id: str | None,
               open_deals: list[dict], email_log_id: str | None) -> str | None:
    """Execute the clustering decision. Returns the deal id the email belongs
    to (existing or newly created), or None. An 'existing' id is only trusted
    if it's genuinely one of the contact's open deals — the model can't route
    mail onto another customer's deal."""
    action = (deal or {}).get("action")
    if action == "existing":
        did = (deal.get("deal_id") or "").strip()
        if any(d["id"] == did for d in open_deals):
            if email_log_id:
                db.crm_set_activity_deal(tenant_id, email_log_id, did)
            return did
        return None
    if action == "new":
        try:
            conf = float(deal.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        title = (deal.get("title") or "").strip()
        if conf < MIN_DEAL_CONFIDENCE or not title:
            return None
        stages = crm.ensure_default_stages(tenant_id)
        first_active = next((s for s in stages if s.get("stage_type") == "active"), None)
        value = deal.get("value_estimate")
        try:
            value = float(value) if value else None
        except (TypeError, ValueError):
            value = None
        row = db.crm_create_deal(tenant_id, {
            "title": title[:200], "primary_contact_id": contact_id,
            "company_id": company_id,
            "stage_id": first_active["id"] if first_active else None,
            "value_estimate": value or None})
        if email_log_id:
            db.crm_set_activity_deal(tenant_id, email_log_id, row["id"])
        return row["id"]
    return None


REINFORCE_CONFIDENCE = 0.6   # below this, treat "reinforce" as "create" (safer)
RESOLVE_CONFIDENCE = 0.75    # auto-resolve at this; 0.5–0.75 = suggested
SUGGEST_RESOLVE_AT = 0.50


def _note_cleared(tenant_id: str, promise: dict | None, *, evidence: str,
                  by_kind: str | None, by_id: str | None, by_label: str | None) -> None:
    """The timeline line for an auto-cleared promise (Kris 2026-09-20), written
    with the row as it now stands. Best-effort: the ledger write already
    happened and a feed hiccup must never undo it."""
    if not promise:
        return
    try:
        crm.note_promise_cleared(tenant_id, {
            **promise, "status": "resolved", "resolution_type": "auto",
            "resolution_evidence": evidence,
            "resolved_by_kind": by_kind, "resolved_by_id": by_id,
            "resolved_by_label": by_label,
            "resolved_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        log.warning("promise-cleared timeline line failed for %s",
                    promise.get("id"), exc_info=True)


def apply_promises(tenant_id: str, promises: list[dict], *, contact_id: str,
                   company_id: str | None, deal_id: str | None,
                   email_log_id: str | None,
                   open_promises: list[dict] | None = None,
                   direction: str | None = None) -> dict:
    """Phase 3.5: promise-aware application — create / reinforce / resolve.
    Returns {"created": n, "reinforced": n, "resolved": n} tally. `direction`
    is the email being read (inbound/outbound) — it names what cleared a
    resolved promise: 'their email' or 'your reply'."""
    tally = {"created": 0, "reinforced": 0, "resolved": 0}
    by_id = {p["id"]: p for p in (open_promises or [])}
    open_ids = set(by_id)
    # The cleared-by link for anything THIS email resolves (2026-09-20).
    cleared_by = {"by_kind": "email" if email_log_id else None,
                  "by_id": email_log_id,
                  "by_label": (("your reply" if direction == "outbound" else "their email")
                               if email_log_id else None)}
    for p in promises or []:
        who = (p.get("who") or "").strip()
        description = (p.get("description") or "").strip()
        if who not in ("us", "them") or not description:
            continue
        action = (p.get("action") or "create").strip()
        confidence = 0.0
        try:
            confidence = float(p.get("confidence") or 0)
        except (TypeError, ValueError):
            pass
        existing_id = (p.get("existing_promise_id") or "").strip()

        if action == "reinforce" and existing_id and existing_id in open_ids:
            if confidence >= REINFORCE_CONFIDENCE:
                db.crm_reinforce_promise(existing_id, tenant_id,
                                         description=description[:300])
                tally["reinforced"] += 1
                continue
            # Low confidence reinforce → treat as new (dedup gate in spec)
            action = "create"

        if action == "resolve" and existing_id and existing_id in open_ids:
            evidence = (p.get("resolution_evidence") or "").strip()
            if confidence >= RESOLVE_CONFIDENCE:
                db.crm_resolve_promise_auto(existing_id, tenant_id,
                                            evidence=evidence[:300], **cleared_by)
                tally["resolved"] += 1
                _note_cleared(tenant_id, by_id.get(existing_id),
                              evidence=evidence[:300], **cleared_by)
                # Hooks mirror resolved customer obligations into registered modules.
                # Each hook owns its tenant gate and best-effort handling.
                if who == "them":
                    for ext in _INTEL_EXTENSIONS.values():
                        if ext["promise_resolved_hook"]:
                            ext["promise_resolved_hook"](tenant_id, existing_id)
            elif confidence >= SUGGEST_RESOLVE_AT:
                db.crm_suggest_promise_resolved(existing_id, tenant_id,
                                                evidence=evidence[:300], **cleared_by)
            continue

        if action == "create" and who == "us" and _DIARY_HOLDS_IT.search(description):
            # The prompt says it; this is the backstop (run log F20, plumber take
            # 2026-09-25: "You owe: hold the Saturday morning, Saturday afternoon
            # and Monday morning slots open for Sarah · Friday 20:00" — the
            # offered holds are the diary's, the owner owes nothing).
            log.info("promise skipped — the diary holds it: %s", description[:120])
            continue
        if action == "create":
            direction_val = "we_owe" if who == "us" else "they_owe"
            due_hint_val = (p.get("due_hint") or "").strip() or None
            created = db.crm_insert_promise(
                tenant_id, contact_id=contact_id, company_id=company_id,
                deal_id=deal_id, direction=direction_val,
                description=description[:300],
                due_hint=due_hint_val, email_log_id=email_log_id)
            if created:
                tally["created"] += 1
                # Phase 4a §19.2: auto-generate a task for outbound promises
                if direction_val == "we_owe":
                    _auto_task_for_promise(tenant_id, description[:300],
                                          contact_id=contact_id, deal_id=deal_id,
                                          due_hint=due_hint_val)
    return tally


# ── action-side promise resolution (Julius 2026-09-03) ───────────────────────
# The email intel pass resolves promises the CUSTOMER's email fulfils; this
# resolves ones the BUSINESS'S OWN ACTION fulfils — a promised booking
# confirmed, the promised invoice sent, the requested visit added to the diary.
# Without it the ledger read "We owe: hold the October weekend" while BK-1017
# sat confirmed in the diary.
_ACTION_RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "promise_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["promise_id", "confidence", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["resolved"],
    "additionalProperties": False,
}

_ACTION_RESOLVE_SYSTEM = (
    "You check whether an action a business's OWN system just performed "
    "fulfils any of that customer's open promises. You are given the action "
    "and the open promises ('us' = the business owes it, 'them' = the "
    "customer owes/requested it). Return ONLY promises this action clearly "
    "COMPLETES — an action can fulfil an 'us' promise it performs (the "
    "promised booking is now confirmed, the promised invoice sent) or a "
    "'them' request it satisfies (the visit they asked for is now in the "
    "diary). Never resolve a partial or conditional match — a promise gated "
    "on something the action does not show ('once X has happened') stays "
    "open. confidence 1.0 = unmistakable. evidence is ONE short line naming "
    "the action. Empty list when nothing is completed.")


async def _action_resolve_llm(tenant_id: str, prompt: str) -> list[dict]:
    """The raw classification call — split out so tests script it."""
    msg = await llm.create(
        client=agent.client(), tenant_id=tenant_id,
        operation="crm_action_resolve",
        model=settings.anthropic_model, max_tokens=400,
        system=_ACTION_RESOLVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema",
                                  "schema": _ACTION_RESOLVE_SCHEMA}},
    )
    usage.record_anthropic(tenant_id, msg.model, msg.usage,
                           operation="crm_action_resolve")
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text").strip()
    try:
        data = json.loads(text)
        return [x for x in (data.get("resolved") or []) if isinstance(x, dict)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def resolve_from_action(tenant_id: str, contact: str | None,
                              action_text: str, client=None, *,
                              ref: tuple | None = None) -> dict:
    """Run the resolve pass for one system ACTION against the contact's open
    promises. `ref` = (kind, id, label) names the thing performing the action
    — ('booking', <id>, 'BK-1017'), ('invoice', <id>, 'INV-1003'), ('quote',
    <id>, 'QUO-1012') — and is stamped on whatever it clears so the card's
    green tick opens it (Kris 2026-09-20). Gated hard: no identified email contact, or no open promises,
    returns at the ledger lookup with ZERO LLM spend — so wiring this into
    every confirm/send costs nothing for the overwhelming majority of them.
    Same confidence gates and evidence trail as the email pass
    (crm_resolve_promise_auto / crm_suggest_promise_resolved). Callers wrap in
    try/except — an intel hiccup must never cost the action."""
    tally = {"resolved": 0, "suggested": 0}
    by_kind, by_id, by_label = (tuple(ref) + (None, None, None))[:3] if ref else (None, None, None)
    cleared_by = {"by_kind": by_kind, "by_id": str(by_id) if by_id else None,
                  "by_label": by_label}
    contact = (contact or "").strip().lower()
    if not contact or "@" not in contact:
        return tally
    c = db.crm_contact_by_email(tenant_id, contact, client=client)
    if not c:
        return tally
    open_p = db.crm_open_promises(tenant_id, contact_id=c["id"], client=client)
    if not open_p:
        return tally
    by_id = {p["id"]: p for p in open_p}
    lines = [f"Action the system just performed: {action_text.strip()[:300]}",
             "", "Open promises with this customer:"]
    for p in open_p:
        side = "us" if p.get("direction") == "we_owe" else "them"
        lines.append(f"- promise_id={p['id']} ({side}): {p.get('description')}")
    for item in await _action_resolve_llm(tenant_id, "\n".join(lines)):
        p = by_id.get(str(item.get("promise_id") or ""))
        if p is None:
            continue   # invented id — never apply
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        evidence = str(item.get("evidence") or "").strip()[:300]
        if conf >= RESOLVE_CONFIDENCE:
            db.crm_resolve_promise_auto(p["id"], tenant_id, evidence=evidence, **cleared_by)
            tally["resolved"] += 1
            _note_cleared(tenant_id, p, evidence=evidence, **cleared_by)
            if p.get("direction") == "they_owe":
                for ext in _INTEL_EXTENSIONS.values():
                    if ext["promise_resolved_hook"]:
                        ext["promise_resolved_hook"](tenant_id, p["id"])
        elif conf >= SUGGEST_RESOLVE_AT:
            db.crm_suggest_promise_resolved(p["id"], tenant_id, evidence=evidence, **cleared_by)
            tally["suggested"] += 1
    return tally


def apply_sentiment(tenant_id: str, sentiment: dict, *, contact_id: str,
                    email_log_id: str | None) -> dict | None:
    """Fold one inbound email's mood read into the contact's rolling state.

    The contact carries mood + trend + the trigger phrase; the email's activity
    row gets stamped so the timeline shows the emotional arc. Trend compares the
    new mood's rank against the stored one — that's the whole point: 'gone from
    neutral to frustrated over three emails' is what a single-email read can't
    say. Low-confidence reads keep the last stored state (a guess must never
    overwrite a solid read). Returns the applied state, or None when nothing
    was applied."""
    mood = (sentiment or {}).get("mood")
    if mood not in MOOD_RANK:
        return None
    if float(sentiment.get("confidence") or 0) < MIN_SENTIMENT_CONFIDENCE:
        return None
    prev = db.crm_get_contact(contact_id, tenant_id) or {}
    prev_mood = prev.get("sentiment")
    prev_rank = MOOD_RANK.get(prev_mood)
    rank = MOOD_RANK[mood]
    if prev_rank is None or rank == prev_rank:
        trend = "steady"
    else:
        trend = "worsening" if rank > prev_rank else "improving"
    trigger = (sentiment.get("trigger") or "").strip()[:200]
    # Which conversation this read came from (Kris 2026-09-01). The contact's
    # rolling mood is right for the CRM and wrong for the Inbox, where it tinted
    # every later message the person sent — a refund row's temper washing over a
    # cheerful enquiry hours afterwards. Recording the thread lets the Inbox
    # decline to colour a different one, without changing what the CRM stores.
    # Best-effort: an unknown thread simply means no Inbox tint.
    thread_id = None
    if email_log_id:
        try:
            thread_id = (db.get_email_log_entry(email_log_id, tenant_id) or {}).get("thread_id")
        except Exception:
            log.warning("could not read thread for sentiment on %s", email_log_id,
                        exc_info=True)
    db.crm_update_contact(contact_id, tenant_id, {
        "sentiment": mood, "sentiment_trend": trend,
        "sentiment_reason": trigger or None,
        "sentiment_thread_id": thread_id,
        "sentiment_updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if email_log_id:
        db.crm_stamp_activity_sentiment(tenant_id, email_log_id, mood)
    return {"mood": mood, "trend": trend, "reason": trigger,
            "temperature": mood_temperature(mood)}


def _auto_task_for_promise(tenant_id: str, description: str, *,
                           contact_id: str | None, deal_id: str | None,
                           due_hint: str | None) -> None:
    """Phase 4a: create a follow-through task when we make a promise."""
    from datetime import date, timedelta
    # Parse due_hint into a date if possible; default +3 business days
    due = None
    if due_hint:
        # Simple heuristics for common phrases
        hint_lower = due_hint.lower()
        today = date.today()
        if "today" in hint_lower:
            due = today
        elif "tomorrow" in hint_lower:
            due = today + timedelta(days=1)
        elif "monday" in hint_lower:
            days_ahead = (7 - today.weekday()) % 7 or 7
            due = today + timedelta(days=days_ahead)
        elif "friday" in hint_lower:
            days_ahead = (4 - today.weekday()) % 7 or 7
            due = today + timedelta(days=days_ahead)
    if not due:
        # Default: +3 business days
        due = date.today() + timedelta(days=3)
    try:
        db.crm_create_task(tenant_id, title=f"Follow through: {description}",
                           due_date=due, priority="normal",
                           contact_id=contact_id, deal_id=deal_id,
                           source="auto_promise")
    except Exception:
        log.warning("Auto-task for promise failed", exc_info=True)


async def process_email(tenant_id: str, *, direction: str, subject: str | None,
                        body: str | None, contact_id: str,
                        company_id: str | None, contact_name: str | None,
                        company_name: str | None, email_log_id: str | None) -> dict:
    """Analyze one email and apply the results. The single entry point both
    hooks call; returns a tally for the agent log."""
    open_deals = [d for d in db.crm_list_deals(tenant_id, contact_id=contact_id)
                  if d.get("status") == "open"]
    # Phase 3.5 §18.3/18.4: load open promises so the LLM can dedup + resolve
    open_promises = db.crm_open_promises(tenant_id, contact_id=contact_id)
    # Capture extensions only from inbound customer words. Outbound calls keep
    # the base schema/token cap and do not consult the per-tenant gates.
    extensions = tuple(ext for ext in _INTEL_EXTENSIONS.values()
                       if ext["wants"](tenant_id)) if direction == "inbound" else ()
    analysis = await analyze(tenant_id, direction=direction, subject=subject,
                             body=body, contact_name=contact_name,
                             company_name=company_name, open_deals=open_deals,
                             open_promises=open_promises,
                             email_log_id=email_log_id, extensions=extensions)
    deal_id = None
    if direction == "inbound":   # clustering is an inbound decision (on-write, §14)
        deal_id = apply_deal(tenant_id, analysis.get("deal"), contact_id=contact_id,
                             company_id=company_id, open_deals=open_deals,
                             email_log_id=email_log_id)
    elif open_deals:             # our reply files onto the freshest open deal
        deal_id = open_deals[0]["id"]
    promise_tally = apply_promises(tenant_id, analysis.get("promises"),
                                   contact_id=contact_id, company_id=company_id,
                                   deal_id=deal_id, email_log_id=email_log_id,
                                   open_promises=open_promises, direction=direction)
    # Active capture hooks run after promises and before sentiment, as before.
    # Each hook owns best-effort handling; results retain their branch ID keys.
    ext_results = {f"{key}_id": None for key in _INTEL_EXTENSIONS}
    if deal_id:
        for ext in extensions:
            if not ext["apply_deal_hook"]:
                continue
            row = ext["apply_deal_hook"](tenant_id, deal_id,
                                         analysis.get(ext["key"]) or {},
                                         email_log_id=email_log_id)
            ext_results[f"{ext['key']}_id"] = (row or {}).get("id")
    # Sentiment: inbound-only (the customer's words, never our own reply's tone).
    # Best-effort like everything here — a mood-write failure never costs the rest.
    sentiment_state = None
    if direction == "inbound":
        try:
            sentiment_state = apply_sentiment(tenant_id, analysis.get("sentiment") or {},
                                              contact_id=contact_id,
                                              email_log_id=email_log_id)
        except Exception:
            log.warning("sentiment apply failed for contact %s", contact_id, exc_info=True)
    return {"deal_id": deal_id, "promises": promise_tally, **ext_results,
            "sentiment": sentiment_state}


async def note_outbound(tenant_id: str, log_id: str) -> None:
    """A reply went out — read our own commitments from it (we_owe promises)."""
    row = db.get_email_log_entry(log_id, tenant_id)
    if not row or not row.get("draft_reply"):
        return
    contact = db.crm_contact_by_email(tenant_id, row.get("from_address") or "")
    if not contact:
        return
    company = contact.get("crm_companies") or {}
    await process_email(
        tenant_id, direction="outbound", subject=row.get("subject"),
        body=row.get("draft_reply"), contact_id=contact["id"],
        company_id=company.get("id") or contact.get("company_id"),
        contact_name=contact.get("name"), company_name=company.get("name"),
        email_log_id=log_id)


# ── Drafter awareness (§5: every reply knows the relationship) ───────────────

def build_crm_context(contact: dict | None, definitions: list[dict],
                      deals: list[dict], promises: list[dict],
                      tenant_id: str | None = None,
                      patient_summary: str = "",
                      activities: list[dict] | None = None,
                      probe_out: list | None = None) -> str:
    """The <customer_context> block for the drafting prompt: who this is, what
    we track about them, live deals and open promises. Pure; '' when the CRM
    knows nothing yet. Phase 3.5: also includes probe instructions for missing
    required fields. ``probe_out`` receives the first field the prompt asks
    the drafter to collect; this is an instruction, not proof of an actual ask.

    Practice OS (docs/os/practice.md §12.2): `patient_summary` is an OPTIONAL, already-
    REDACTED one-liner (db.patient_history_summary — insurer/recall/last-visit + a
    'has notes on file' line, NEVER raw clinical text). It's populated only on the practice
    path and appended when present, so this stays byte-identical for every non-practice
    caller (they pass nothing). Raw medical notes never reach this function."""
    if not contact:
        return ""
    company = contact.get("crm_companies") or {}
    lines = []
    who = contact.get("name") or contact.get("email")
    if who:
        lines.append(f"Customer: {who}" + (f" at {company.get('name')}"
                                           if company.get("name") else ""))
    # Their phone (Julius 2026-09-03): on file means never re-asked — the
    # drafter can key it straight into a booking's required details.
    if contact.get("phone"):
        lines.append(f"Phone: {contact['phone']}")
    # Customer mood (sentiment addendum): the register instruction. Only when the
    # relationship is running warm/hot — a calm customer gets no line at all, so
    # the vast majority of drafts are byte-identical to before.
    mood = contact.get("sentiment")
    if mood in ("concerned", "frustrated", "angry"):
        trend = contact.get("sentiment_trend")
        reason = contact.get("sentiment_reason")
        line = f"Customer mood: {mood}"
        if trend and trend != "steady":
            line += f" and {trend}"
        if reason:
            line += f" ({reason})"
        line += ". "
        if mood == "concerned":
            line += ("Reassure them: be specific about what happens next and when. "
                     "No selling.")
        else:
            line += ("Acknowledge their frustration FIRST, in your own words. "
                     "Apologise once if we are at fault — no excuses. Be brief, "
                     "direct and concrete about what happens next. Absolutely no "
                     "selling or upbeat filler.")
        lines.append(line)
    # Special-category (health / medical / allergy) fields never reach the AI
    # (security review M39) — the owner reads them on the card, the drafter
    # never sees them.
    by_key = {(d["entity"], d["key"]): d["label"] for d in definitions
              if not crm_fields.is_special_category(d)}
    for entity, record in (("contact", contact), ("company", company)):
        for key, value in (record.get("custom_data") or {}).items():
            label = by_key.get((entity, key))
            if label and value not in (None, ""):
                lines.append(f"{label}: {value}")
    for d in deals:
        if d.get("status") != "open":
            continue
        stage = (d.get("crm_stages") or {}).get("name")
        lines.append(f"Open deal: {d.get('title') or 'Untitled'}"
                     + (f" — stage: {stage}" if stage else ""))
    for p in promises:
        side = "We owe them" if p.get("direction") == "we_owe" else "They owe us"
        due = f" ({p['due_hint']})" if p.get("due_hint") else ""
        lines.append(f"{side}: {p.get('description')}{due}")
    # Recent NON-mail activity (2026-07-31): what this customer actually DID —
    # portal questions, self-serve bookings/reschedules, invoices, quotes. The
    # email/SMS rows are excluded on purpose: the thread history already
    # carries them, and repeating them here would just burn prompt tokens.
    if activities:
        recent = [a for a in activities
                  if a.get("activity_type") not in ("email", "sms")][:6]
        if recent:
            lines.append("Recent activity (newest first):")
            for a in recent:
                when = (a.get("occurred_at") or "")[:10]
                desc = (a.get("summary") or a.get("subject")
                        or a.get("activity_type") or "").strip()
                if desc:
                    lines.append(f"- {when}: {desc}")
    # Practice OS §12.2: the REDACTED patient-history summary — only ever present on the
    # practice path, and only the drafter-safe line (never raw clinical notes).
    if patient_summary and patient_summary.strip():
        lines.append(f"Patient history: {patient_summary.strip()}")
    # Phase 3.5 §18.2: probe instructions for missing high-requirement fields
    if tenant_id:
        try:
            missing = crm_fields.get_missing_probeable_fields(
                tenant_id, contact["id"], company_id=company.get("id"))
            probe_block = crm_fields.build_probe_instructions(missing)
            if probe_block:
                lines.append("")
                lines.append(probe_block)
                if probe_out is not None:
                    probe_out.append(missing[0])
        except Exception:
            pass  # probing is best-effort, never breaks the draft
    return "\n".join(lines) if len(lines) > 1 or deals or promises else ""
