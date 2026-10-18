"""Incoming-email → AI draft pipeline.

Shared by the Graph webhook (automatic) and the manual dashboard triggers. Draft-only
by default: it produces a pending row in email_log for a human to approve. A tenant can
opt into full-auto (tenant_config.auto_send): on the automatic inbound path only, a
non-escalation reply is then sent immediately and the row marked 'sent'.

Enterprise multi-mailbox (D5): `mailbox_id` is the connection that RECEIVED the
inbound. It selects the brain (tenant config ⊕ that mailbox's overrides, plus
its scoped knowledge), stamps the email_log row, and sends the reply from the
same address. None — the SaaS path — means the tenant's default mailbox and is
behaviourally identical to before.
"""
import logging
import re

from app import (actor, agent, calendar_sync, crm, crm_extract, crm_fields, crm_intel, db, diary_config, disclosure,
                 events, intake, mailbox, party_extract, push, quote_autodraft, scheduling, sms,
                 suppression, textfmt, trace, trial, usage, working)
from app import knowledge as kb
from app.config import settings
from app.booking_errors import VerificationUnavailable
from app.conversation_direction import direction, replayable_history
from app.dates import pretty_date

log = logging.getLogger(__name__)

# In-flight draft claims, keyed (tenant_id, draft key) — the pre-token
# idempotency gate. The push webhook and the in-process safety poller share
# this one process and can both pick up the same unseen message within
# seconds; each then races into a full draft loop (twin request_ids in prod,
# 2026-08-31 — real diary holds taken twice) before insert_email_log's
# (tenant, source_message_id) dedupe collapses them to one row. The claim is
# taken in draft_for_email before anything that costs a token and released in
# its finally; whoever claims second walks away with None.
_drafts_in_flight: set[tuple[str, str]] = set()


# The no-reply rule lives with the other kinds now (src/app/inbox_rules.py, Kris
# 2026-09-13); this name stays for its callers (crm, inbox_labels).
from app.inbox_rules import is_no_reply_address  # noqa: E402


def _inbox_rules(tenant_id: str) -> dict:
    """This tenant's generic-ignore switches (Settings › Automated mail).
    Best-effort: a config read that fails leaves the defaults on."""
    from app import inbox_rules
    try:
        return inbox_rules.settings_of(db.get_tenant_config(tenant_id) or {})
    except Exception:
        log.warning("inbox rules read failed for %s — defaults", tenant_id, exc_info=True)
        return dict(inbox_rules.DEFAULTS)


# Payment-confirmation detection (invoicing directive §5.1) — rule-based, pre-LLM.
# Known payment-processor sender domains + the phrases their receipts lead with.
_PAYMENT_SENDER_RE = re.compile(
    r"@(?:[a-z0-9-]+\.)*(?:stripe\.com|paypal\.(?:com|co\.uk)|gocardless\.com|"
    r"sumup\.(?:com|co\.uk)|squareup\.com|wise\.com)$", re.I)
_PAYMENT_PHRASES = ("payment received", "payment confirmation", "payment successful",
                    "you've been paid", "you have been paid", "remittance advice",
                    "has paid your invoice", "receipt for your payment")
_INVOICE_REF_RE = re.compile(r"\bINV-\d{3,}\b", re.I)


def _cfg_draft_hold(tenant_id: str) -> bool:
    """True while this tenant has drafting held (the go-live hold above).

    Best-effort on purpose: a config read that fails must not stop the mail being
    handled, and the safe direction when we cannot tell is to behave as before —
    draft. A tenant wrongly held would look like the product silently doing
    nothing, which is far worse than a few tokens spent."""
    try:
        return bool((db.get_tenant_config(tenant_id) or {}).get("draft_hold"))
    except Exception:
        log.warning("draft-hold check failed for %s — drafting anyway", tenant_id,
                    exc_info=True)
        return False


def looks_like_payment_confirmation(from_address: str | None, subject: str | None,
                                    body: str | None) -> bool:
    """A machine payment receipt (Stripe/PayPal/bank 'payment received' mail) —
    categorise, link to the invoice, never draft. Sender-domain match OR a
    receipt phrase in the subject/body head."""
    if _PAYMENT_SENDER_RE.search((from_address or "").strip().lower()):
        return True
    text = f"{(subject or '')} {(body or '')[:1500]}".lower()
    return any(p in text for p in _PAYMENT_PHRASES)


def find_invoice_ref(subject: str | None, body: str | None) -> str | None:
    m = _INVOICE_REF_RE.search(f"{subject or ''}\n{(body or '')[:4000]}")
    return m.group(0).upper() if m else None


def _handle_payment_confirmation(tenant_id: str, email: dict, *, source_message_id,
                                 mailbox_id, channel) -> None:
    """Log a payment-confirmation inbound: 'automated' when it names an invoice we
    hold (linked by ref in the reason), 'fyi' otherwise (a receipt for the owner's
    records). If the named invoice is still unpaid and Stripe didn't already settle
    it (no webhook), surface it — 'want me to mark it as paid?'."""
    from_address = email.get("from_address")
    ref = find_invoice_ref(email.get("subject"), email.get("body"))
    inv = db.get_invoice_by_ref(tenant_id, ref) if ref else None
    if inv:
        category, reason = "automated", f"Payment confirmation for invoice {ref}"
    elif ref:
        category, reason = "fyi", f"Payment confirmation mentioning {ref} — no matching invoice"
    else:
        category, reason = "fyi", "Payment receipt — kept for your records"
    try:
        log_id, created = db.insert_email_log(
            tenant_id, from_address, email.get("subject"), email.get("body"), "",
            status="skipped", source_message_id=source_message_id,
            thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
            from_name=email.get("from_name"), channel=channel,
            category=category, category_reason=reason)
    except Exception:
        log.warning("failed to log payment confirmation from %s", from_address,
                    exc_info=True)
        return
    if not created:
        return
    trace.emit(tenant_id, "categorisation", email_log_id=log_id, contact=from_address,
               detail={"category": category, "method": "rule",
                       "trigger": "email_received", "reason": reason,
                       "invoice_ref": ref})
    # Manual-payment surface: the webhook path stamps stripe_payment_intent_id when
    # IT settles an invoice; a confirmation arriving without that is a bank
    # transfer / off-platform payment the owner should confirm.
    if inv and inv.get("status") in ("sent", "overdue", "partially_paid") \
            and not inv.get("stripe_payment_intent_id"):
        db.log_agent_event(
            tenant_id, "invoice_payment_seen",
            f"Received a payment confirmation for {ref} — want me to mark it as paid?",
            contact=inv.get("customer_email"), email_log_id=log_id, client=None)
        try:
            push.notify(tenant_id,
                        title=f"Payment confirmation for {ref}",
                        body="Looks like this invoice was paid — mark it as paid?",
                        url=f"/app/invoices/{inv['id']}",
                        tag=f"invoice-confirm-{inv['id']}", kind="invoice_payment_seen",
                        contact=inv.get("customer_email"),
                        contact_name=inv.get("customer_name"))
        except Exception:
            log.warning("payment-seen push failed", exc_info=True)


def _classify(model_category: str, *, escalated: bool, scheduling_gap: bool,
              deferred: bool, thread_state: str, held: bool) -> tuple[str, bool]:
    """The smart inbox label for a freshly drafted inbound → (category, informational).

    The model only judged the inbound itself (needs_reply | fyi | automated); the
    ground-truth signals the pipeline computed win over it, in the same precedence
    thread_state uses: a human must act (escalation / scheduling gap / a deferred
    fact / model-reported needs_attention) → needs_reply; a hold actually placed
    this run → booking_pending; a clean fyi/automated call → keep it, and flag it
    `informational` so the caller keeps the row OUT of the approval queue; an
    ordinary queued draft → draft_ready. An informational call is only honoured
    when NONE of the action signals fired — anything a human should see beats
    'no action needed'."""
    if escalated or scheduling_gap or deferred or thread_state == "needs_attention":
        return "needs_reply", False
    if held:
        return "booking_pending", False
    if model_category in ("fyi", "automated"):
        return model_category, True
    return "draft_ready", False


_UNREAD_SETTINGS = object()


def _draft_settings(tenant_id, mailbox_id, client=None):
    """Read required settings before drafting or releasing the previous proposal."""
    try:
        cfg = db.effective_config(tenant_id, mailbox_id, client=client)
        mods = db.active_modules(tenant_id)
        booking_cfg = (db.get_scheduling_config(tenant_id, client=client)
                       if "scheduling" in mods or not ({"tms", "fsm"} & mods) else None)
        return cfg, booking_cfg
    except Exception as exc:
        raise VerificationUnavailable("the business settings") from exc


def _hold_unreadable_settings(tenant_id, email, mailbox_id, reason, *,
                              entry=None, source_message_id=None, channel="email", client=None):
    """Keep the inbound visible and empty, using the existing verification hold."""
    result = agent.DraftResult(text="", usage={}, model="", category="needs_reply")
    state = {"verification_unavailable": str(reason), "bookings": [], "calls": []}
    result = _verification_hold(result, state)
    fields = dict(thread_state=result.thread_state, category="needs_reply",
                  action_summary=result.action_summary, category_reason=result.category_reason,
                  knowledge_gap=None, draft_sources=None,
                  draft_reasoning=_working_panel(state, result, {}, draft="", needs_owner=True))
    if entry is not None:
        log_id = entry["id"]
        # A verification hold must never erase text that was already sent.
        held_text = (entry.get("draft_reply") or "") if redraft_is_text_only(entry) else ""
        db.update_email_log_draft(log_id, held_text, tenant_id, client=client, **fields)
    else:
        log_id, _ = db.insert_email_log(
            tenant_id, email.get("from_address"), email.get("subject"), email.get("body"), "",
            status="pending", source_message_id=source_message_id,
            thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
            from_name=email.get("from_name"), channel=channel, **fields)
        try:
            crm.ingest_email_log_row(tenant_id, {"id": log_id, **email, "status": "pending"})
        except Exception:
            log.warning("CRM ingest failed for settings-held inbound %s", log_id, exc_info=True)
    _announce_held(tenant_id, log_id, email.get("from_address"), mailbox_id)
    return "" if entry is not None else log_id


def _customer_draft_context(tenant_id, email, existing_bookings, *, channel="email", client=None,
                            probe_out=None):
    """Same customer facts and authority for initial replies and redrafts."""
    contact = email.get("from_address")
    party = "\n".join(x for x in (
        _party_context(tenant_id, contact, existing_bookings, client=client),
        _documents_context(tenant_id, contact, client=client),
        _after_job_context(tenant_id, contact)) if x)
    crm_context = _crm_context(tenant_id, contact, channel=channel, client=client,
                               probe_out=probe_out)
    shop = ""
    if db.module_active(tenant_id, "storefront"):
        try:
            row = (db.crm_contact_by_phone(tenant_id, contact, client=client) if channel == "sms"
                   else db.crm_contact_by_email(tenant_id, contact, client=client)) if contact else None
            text = f"{email.get('subject', '')} {email.get('body', '')}".lower()
            mentioned = [p["name"] for p in db.shop_list_products(tenant_id)
                         if p.get("name") and p["name"].lower() in text]
            shop = db.shop_storefront_context(tenant_id, (row or {}).get("id"), mentioned)
        except Exception:
            log.warning("storefront context build failed for %s", contact, exc_info=True)
    credit_refusal = False
    if db.module_active(tenant_id, "wholesale"):
        try:
            from app import wholesale
            credit = wholesale.inbound_credit_context(tenant_id, contact)
            if credit:
                crm_context = "\n\n".join(x for x in (crm_context, credit) if x)
                credit_refusal = True
        except Exception:
            log.warning("wholesale credit context failed for %s", contact, exc_info=True)
    return party, crm_context, shop, credit_refusal


def _scheduling_setup(tenant_id: str, contact: str | None, thread_id: str | None,
                      mailbox_id: str | None, client=None,
                      existing_bookings: list | None = None,
                      email: dict | None = None, history: list | None = None,
                      owner_directed: bool = False, booking_cfg=_UNREAD_SETTINGS):
    """Module-gated tooling for one draft call — the data gate (D3/D9) in one place for
    both the draft and redraft paths. Returns (tools, executor, state, diary_summary,
    flags) — all None/empty when no relevant module is active.

    The base agent gets (docs/modules/module-agent-handoff.md, Phase 1):
      scheduling on   → the OFFICE diary tools (windows) + executor; the office
                        diary_summary reaches the prompt.
      tms on          → the ONE hand-off tool, ask_transport; its async executor invokes
                        the TMS specialist agent, which owns the fleet capacity tools,
                        places the hold, and returns a structured answer. The base never
                        sees fleet tools or transport rules.
      fsm on          → the ONE hand-off tool, ask_field_service; its async executor
                        invokes the field-service specialist (the trades twin of TMS),
                        which owns the team member capacity tools. fsm SUPERSEDES the base
                        office diary (a trades tenant books every job via the specialist).
      scheduling+tms  → both, in one composite executor (a logistics tenant: base does
                        office, hands transport off — spec D8).

    `state['bookings']` aggregates holds from BOTH paths (office + transport) so the
    pipeline links/resolves them exactly as before; `state['usage']` carries the
    specialist's tokens so they're metered onto the draft.

    Office scheduling is gated on its INTAKE being configured (the "block the diary until
    set up" onboarding): until the business defines what a booking needs, the office tools
    are withheld — so the agent never schedules without the tenant's required details. The
    intake schema is injected into the prompt (flags['intake']) AND enforced in the office
    executor (intake_fields). TMS transport is unaffected (it has its own required-field
    gates).

    `existing_bookings` — the contact's live diary rows the caller already read for the
    prompt — rides into the office executor's live-diary duplicate gate, so a hold for a
    visit the diary already holds (2026-09-20: two confirmed stays re-held on a
    details-only follow-up) is refused with its reference instead of placed again."""
    from app.agents import tms as tms_agent
    from app.agents import field_service as fs_agent
    from app import storefront as storefront_mod

    mods = db.active_modules(tenant_id)
    tms_on = "tms" in mods
    fsm_on = "fsm" in mods
    sched_on = "scheduling" in mods
    # Storefront OS (docs/os/storefront.md §6): stock-aware drafter tools, injected ONLY
    # when the module is active — exactly how the diary tools are conditionally injected.
    # It rides alongside the office diary (a shop with an appointments diary) and is
    # orthogonal to tms/fsm (a shop is not a transport operator), so it never supersedes them.
    storefront_on = "storefront" in mods
    # Load the booking-requirements config when it can matter: scheduling on, OR the pure
    # email-only base (no tms/fsm) where those requirements drive gather-don't-book.
    cfg_row = booking_cfg
    if cfg_row is _UNREAD_SETTINGS:
        cfg_row = (db.get_scheduling_config(tenant_id, client=client)
                   if (sched_on or not (tms_on or fsm_on)) else None)
    intake_fields = (cfg_row or {}).get("intake_fields") or []
    # Field service SUPERSEDES the base office diary (docs/modules/module-field-service.md): a
    # trades tenant books every job through the field-service specialist, so the base
    # never also runs the office windows diary (the fleet/windows supersede pattern).
    office_on = (sched_on and bool((cfg_row or {}).get("intake_configured_at"))
                 and not fsm_on)
    # Gather-don't-book: the tenant defined booking requirements but no live booking
    # surface exists — either email-only (no booking module), or scheduling ACTIVE but
    # its intake setup never finished (module bought, setup screens skipped — any tenant
    # can land in that half-state). Gating on office_on (configured) rather than sched_on
    # (subscribed) is what stops the half-state falling through to the <booking>
    # no-capability punt ("a colleague will be in touch") — the agent at least gathers
    # the defined fields in draft-first replies; there are no tools — the intake rides
    # in flags. With no intake_fields either, the honest punt stands (nothing to gather).
    gather_on = (not office_on and not tms_on and not fsm_on and bool(intake_fields))
    flags = {"scheduling": office_on, "transport": tms_on, "field_service": fsm_on,
             "storefront": storefront_on, "intake": "", "gather_intake": gather_on}
    if gather_on:
        flags["intake"] = intake.render_for_gather(intake_fields)
    if sched_on and not office_on and not fsm_on:
        from app.booking_policy import compile_policy
        policy = compile_policy(cfg_row)
        if not policy["ready"]:
            flags["booking_policy"] = policy
            flags["intake"] = ""
            flags["gather_intake"] = False
    if not (tms_on or fsm_on or office_on or storefront_on):
        return None, None, None, None, flags

    tools: list[dict] = []
    office_exec = office_state = None
    handoff_exec = handoff_state = None   # tms (ask_transport)
    fs_exec = fs_state = None             # fsm (ask_field_service)
    shop_exec = shop_state = None         # storefront (check_stock / reserve_stock)
    diary = None
    if office_on:
        # Office diary flavour (one of three, chosen from tenant config):
        #   stays    — stays/hire: multi-day arrival→departure spans (intake toggle)
        #   services — per-service durations: the booked length is the service's (service_types)
        #   default  — one fixed slot length (SCHEDULING_TOOLS), byte-identical to before
        # Stays win over services if both are somehow set. Each gets its own tools + diary line.
        from app import services, booking_policy
        profile = booking_policy.compile_profile(cfg_row, tenant_id=tenant_id, mailbox_id=mailbox_id)
        stay = profile.stay
        service_types = (cfg_row or {}).get("service_types") or []
        svc = profile.services
        tools = list(scheduling.STAY_TOOLS if stay
                     else (scheduling.SERVICE_TOOLS if svc else scheduling.SCHEDULING_TOOLS))
        # A stays diary's APPOINTMENTS (Kris 2026-09-13): their own two tools
        # beside the stay tools, run by the service path in its own pool.
        appts = profile.appointments
        if appts:
            tools += list(scheduling.APPOINTMENT_TOOLS)
        # The exact `details` keys ride the tool itself (Kris 2026-09-04): a
        # first-call key mismatch is a whole extra model turn per booking.
        tools = intake.with_detail_keys(tools, intake_fields,
                                        services.names(service_types) if appts else None)
        # Dependants (docs/modules/module-requirement-groups.md): the booking tools take
        # `subjects` in the tenant's words, and the executor gates per instance.
        from app import requirement_groups as rgroups
        dep_groups = rgroups.clean_groups(db.list_requirement_groups(tenant_id))
        # The waiting list (src/app/courses.py): the join tool rides only when the
        # tenant keeps a list — nothing shows for one that has not switched it on.
        from app import courses as _courses
        if _courses.waitlist_on(cfg_row):
            tools.append(scheduling.JOIN_WAITLIST_TOOL)
        # Cancelling (run #8): the customer's own booking, by its reference — the
        # tool applies the cancellation rule and writes the register, so the
        # reply never has to say "a colleague will arrange it".
        tools.append(scheduling.CANCEL_BOOKING_TOOL)
        # Moving (the last open P1 from runs #6/#7): in place when in time, a
        # late cancel plus a fresh hold inside the notice rule.
        tools.append(scheduling.MOVE_BOOKING_TOOL)
        tools = rgroups.with_subjects(tools, dep_groups)
        # Make-ups (src/app/makeups.py): the hold takes `makeup_for` only when the rule is on.
        from app import makeups as _makeups
        if _makeups.on(cfg_row):
            tools = _makeups.with_makeups(tools)
        # When it's full (docs/plan-when-its-full.md): the business's choice
        # decides what the agent is TOLD — the tools here, the rules in the
        # prompt (flags → agent._diary_rules). "hold" leaves both untouched.
        flags["other_dates"] = _courses.settings_of(cfg_row)["other_dates"]
        tools = scheduling.tools_for_other_dates(tools, flags["other_dates"])
        # Grounded booking (docs/plan-grounded-booking.md): when the tenant
        # has opted in, the executor is told which dates this draft may act
        # on — built from the email, the thread and the live bookings the
        # caller already read. Off (the default) passes None: byte-identical.
        _grounding = None
        if (cfg_row or {}).get("grounded_dates"):
            from datetime import datetime as _dt
            from app import grounding as _grounding_mod
            _grounding = _grounding_mod.build(
                subject=(email or {}).get("subject") or "",
                body=(email or {}).get("body") or "",
                history=history or [], existing_bookings=existing_bookings or [],
                today=_dt.now(scheduling.DIARY_TZ).date())
        office_exec, office_state = scheduling.make_executor(
            tenant_id, contact=contact, thread_id=thread_id, mailbox_id=mailbox_id,
            intake_fields=intake_fields, stay=stay,
            service_types=(service_types if (svc or appts) else None),
            groups=dep_groups, existing_bookings=existing_bookings,
            grounding=_grounding, owner_directed=owner_directed, booking_profile=profile)
        # Grounded booking: with no date on record anywhere in the conversation
        # there is no diary work to do at all — nothing to check, hold, move or
        # cancel, because a live booking would have grounded its own dates — so
        # the model is handed NO diary tools and the prompt drops the rules
        # that order it to go and act. Taking away only the BOOKING tools was
        # tried first and it simply reached for what was left, inventing a
        # reference to cancel (live 2026-09-22).
        if _grounding is not None:
            from app import grounding as _grounding_mod
            if _grounding_mod.no_dates(_grounding):
                tools = []
                flags["no_dates"] = True
        diary = (scheduling.stay_diary_line(cfg_row, service_types if appts else None) if stay
                 else (scheduling.service_diary_line(cfg_row, service_types) if svc
                       else scheduling.diary_line(cfg_row)))
        # Inbound-business depth (resource types, stay pricing, per-day hours, party size,
        # recurring, delivery) — describe whatever the tenant configured so the agent uses
        # the matching tool arguments. Empty config adds nothing (parity).
        from app import diary_config
        depth = diary_config.render_all_for_prompt(cfg_row)
        if depth:
            diary = f"{diary}\n{depth}"
        flags["intake"] = intake.render_for_prompt(intake_fields)
        dep_block = rgroups.render_for_prompt(dep_groups)
        if dep_block:
            flags["intake"] = (flags["intake"] + "\n\n" + dep_block) if flags["intake"] else dep_block
        policy = profile.as_policy()
        flags["booking_policy"] = policy
        if not policy["ready"]:
            tools = [scheduling.BOOKING_HANDOFF_TOOL]
            flags["scheduling"] = False
            flags["intake"] = ""
            diary = policy["message"]
    if tms_on:
        tools = tools + [tms_agent.ASK_TRANSPORT_TOOL]
        handoff_exec, handoff_state = tms_agent.make_handoff(
            tenant_id, contact=contact, thread_id=thread_id, mailbox_id=mailbox_id,
            client=client)
    if fsm_on:
        tools = tools + [fs_agent.ASK_FIELD_SERVICE_TOOL]
        fs_exec, fs_state = fs_agent.make_handoff(
            tenant_id, contact=contact, thread_id=thread_id, mailbox_id=mailbox_id,
            client=client)
    if storefront_on:
        # The stock tools (check_stock / reserve_stock). reserve_stock resolves the CRM
        # contact so a reservation hangs off the customer's contact record (§1).
        contact_id = (db.crm_get_or_create_contact(tenant_id, contact).get("id")
                      if contact and "@" in contact else None)
        tools = tools + list(storefront_mod.STOREFRONT_TOOLS)
        shop_exec, shop_state = storefront_mod.make_storefront_executor(
            tenant_id, contact_id=contact_id, thread_id=thread_id)

    state: dict = {"bookings": [], "usage": {}}
    # Grounded booking (docs/plan-grounded-booking.md): the executor's allowed-
    # date set rides on the state from the START, not after the first tool call
    # — the extractor reads it BEFORE the draft, and a draft that calls nothing
    # still wants the ⓘ header. (Live 2026-09-22: the first grounded draft had
    # neither, because the closure held it and this dict never did.)
    if office_state and office_state.get("grounding") is not None:
        state["grounding"] = office_state["grounding"]

    def _sync():
        bookings = []
        if office_state:
            bookings += office_state["bookings"]
            # Out-of-hours times held back for the owner (D1): the pipeline's
            # Today question reads them from here — the merged state is the
            # only one it sees (live 2026-09-26: Ann's never reached Today).
            state["ooh_withheld"] = office_state.get("ooh_withheld") or []
            state["booking_unsupported"] = office_state.get("booking_unsupported")
            state["verification_unavailable"] = office_state.get("verification_unavailable")
        # ACCUMULATE specialist usage (don't assign): with tms and fsm both
        # active, assignment would let the second specialist's tokens replace
        # the first's and under-meter the draft.
        merged_usage: dict = {}
        if handoff_state:
            bookings += handoff_state["bookings"]
            for k, v in (handoff_state.get("usage") or {}).items():
                merged_usage[k] = merged_usage.get(k, 0) + v
            if handoff_state.get("result") is not None:
                state["transport_result"] = handoff_state["result"]
        if fs_state:
            bookings += fs_state["bookings"]
            for k, v in (fs_state.get("usage") or {}).items():
                merged_usage[k] = merged_usage.get(k, 0) + v
            if fs_state.get("result") is not None:
                state["field_service_result"] = fs_state["result"]
        if merged_usage:
            state["usage"] = merged_usage
        state["bookings"] = bookings
        # Storefront reservations are transactions, not bookings — kept separate so the
        # existing booking link/resolve/calendar paths never see them, but surfaced so the
        # pipeline can link them to the draft row (and release on a duplicate webhook).
        if shop_state:
            state["reservations"] = shop_state["reservations"]

    # Live ticker (Kris 2026-08-31): each tool call announces itself on the SSE
    # stream the instant it starts, so an open thread watches the work happen.
    # BOTH halves since 2026-09-21 (src/app/working.py): the static name-keyed label
    # threw the arguments and the result away, so a hold the diary REFUSED went
    # up as "Holding a diary slot…" — the ghost saying the opposite of what
    # happened. The intent line still goes first (the work should look live),
    # and the outcome replaces it the moment the tool answers.
    # `calls` is the same trail in structured form, for the ⓘ panel.
    calls: list = []
    state["calls"] = calls

    def _done(name: str, args: dict, out):
        calls.append((name, args, out))
        events.drafting_step(tenant_id, contact, working.outcome(name, args, out))
        return out

    def execute(name: str, args: dict):
        events.drafting_step(tenant_id, contact, working.intent(name, args))
        # The hand-off tools are async (they await a nested specialist); the office tools
        # are sync. draft_reply awaits whatever is awaitable, so we can return either.
        if name == "ask_transport" and handoff_exec is not None:
            async def _run():
                out = await handoff_exec(name, args)
                _sync()
                return _done(name, args, out)
            return _run()
        if name == "ask_field_service" and fs_exec is not None:
            async def _run():
                out = await fs_exec(name, args)
                _sync()
                return _done(name, args, out)
            return _run()
        # Every office tool rides here — a new tool that is not listed answers
        # "unknown_tool" to the model (run #8: cancel_booking shipped without it
        # and the first live cancel died on exactly that).
        if office_exec is not None and name in ("check_availability", "hold_booking",
                                                "offer_slots", "offer_stay", "join_waitlist",
                                                "cancel_booking", "move_booking",
                                                "check_appointment_availability",
                                                "hold_appointment", "handoff_booking_to_owner"):
            out = office_exec(name, args)
            _sync()
            return _done(name, args, out)
        if shop_exec is not None and name in ("check_stock", "reserve_stock"):
            out = shop_exec(name, args)
            _sync()
            return _done(name, args, out)
        return _done(name, args, {"error": "unknown_tool",
                                  "message": f"No tool named {name}."})

    return tools, execute, state, diary, flags


def _verification_hold(result, sched_state):
    """Tool truth wins even when the model ignores an unavailable check."""
    changes = {}
    unsupported = (sched_state or {}).get("booking_unsupported")
    if unsupported:
        changes.update(text="", blocked=unsupported["message"], deferred=False, missing="",
                       thread_state="needs_attention",
                       action_summary="Booking unsupported — handed to the owner; nothing reserved.")
    reason = (sched_state or {}).get("verification_unavailable")
    if reason:
        changes.update(text="", blocked=reason, deferred=False, missing="",
                       thread_state="needs_attention",
                       action_summary="I couldn't complete a required check — please review",
                       category_reason=reason + " Retry with Redraft when the check is available.")
    if hasattr(result, "_replace"):
        return result._replace(**changes)
    for key, value in changes.items():
        setattr(result, key, value)
    return result


def _gate_gap(sched_state) -> str:
    """What a requirement gate refused for, if that is what stopped this draft.

    An empty draft used to be filed as "a setting blocks this request — fix it
    in Diary settings", which on 2026-09-21 sent Kris looking for a broken
    setting when the truth was that Bella's booster had lapsed. A gate refusal
    is the customer's homework, not the owner's. '' when no gate refused."""
    for name, _args, out in ((sched_state or {}).get("calls") or []):
        if isinstance(out, dict) and out.get("error") == "missing_info":
            missing = out.get("missing")
            if isinstance(missing, list) and missing:
                return "; ".join(str(m) for m in missing)[:200]
    return ""


async def _ground_extract(tenant_id: str, sched_state, email: dict | None) -> None:
    """Grounded booking's perception step (src/app/booking_extract.py): widen the
    allowed dates with ones the model can QUOTE from the email. Only runs when
    the executor was built with a grounding (the tenant opted in); a failure
    leaves the deterministic set standing. Spends nothing otherwise."""
    g = (sched_state or {}).get("grounding") if isinstance(sched_state, dict) else None
    if g is None:
        return
    from datetime import datetime as _dt
    from app import booking_extract
    await booking_extract.enrich(tenant_id, g, subject=(email or {}).get("subject") or "",
                                 body=(email or {}).get("body") or "",
                                 today=_dt.now(scheduling.DIARY_TZ).date())


def _working_panel(sched_state, result, retrieval=None, *, draft=None, needs_owner=None) -> str | None:
    """Keep plain English and the full technical record in the existing field."""
    calls = (sched_state or {}).get("calls") if isinstance(sched_state, dict) else None
    g = (sched_state or {}).get("grounding") if isinstance(sched_state, dict) else None
    from app import grounding
    return working.record(calls, result, retrieval, draft=draft, needs_owner=needs_owner,
                          grounding_note=grounding.note(g) if g is not None else "")


def _merge_handoff_usage(result, sched_state) -> None:
    """Fold the TMS specialist's token usage (recorded in the executor state during a
    transport hand-off) into the draft's usage, so a transport email meters BOTH the
    base call and the nested specialist call under the one draft. No-op when nothing
    handed off. `result.usage` is the summed-totals dict the tool loop returns."""
    extra = (sched_state or {}).get("usage") if isinstance(sched_state, dict) else None
    if not extra or not isinstance(result.usage, dict):
        return
    for k, v in extra.items():
        result.usage[k] = (result.usage.get(k, 0) or 0) + v


def _transport_summary(tr: dict) -> tuple[str, str | None]:
    """One human line + the calendar date (for the Agent Log) from a transport hand-off
    result. The date is set only for a placed hold — that's what lands on the calendar."""
    status, ref, date = tr.get("status"), tr.get("booking_ref"), tr.get("date")
    if status == "held":
        sp = tr.get("spaces")
        extra = f" for {date}" if date else ""
        if sp:
            extra += f", {scheduling.fmt_spaces(sp)} pallet spaces"
        return f"Asked the transport desk — held {ref or 'a booking'}{extra}", (date or None)
    if status == "offered":
        alts = ", ".join(tr.get("alternatives") or [])
        return f"Asked the transport desk — offered {alts or 'alternative dates'}", None
    if status == "needs_info":
        return f"Asked the transport desk — needs info: {tr.get('ask') or 'a question for the customer'}", None
    if status == "no_fit":
        return "Asked the transport desk — load won't fit as one booking; a colleague will arrange", None
    return "Asked the transport desk — temporarily unavailable; a colleague will confirm", None


def _field_service_summary(fr: dict) -> tuple[str, str | None]:
    """One human line + the calendar date (for the Agent Log) from a field-service
    hand-off result — the trades twin of _transport_summary. The date is set only for a
    placed hold (that's what lands on the calendar)."""
    status, ref, date = fr.get("status"), fr.get("booking_ref"), fr.get("date")
    job = fr.get("job_type") or "a job"
    if status == "held":
        extra = f" for {date}" if date else ""
        pr = " (emergency)" if fr.get("priority") == "emergency" else ""
        return f"Asked the field-service desk — held {ref or 'a booking'} for {job}{extra}{pr}", (date or None)
    if status == "offered":
        alts = ", ".join(fr.get("alternatives") or [])
        return f"Asked the field-service desk — offered {alts or 'alternative dates'}", None
    if status == "needs_info":
        return f"Asked the field-service desk — needs info: {fr.get('ask') or 'a question for the customer'}", None
    if status == "no_fit":
        return "Asked the field-service desk — can't be staffed/done; a colleague will arrange", None
    return "Asked the field-service desk — temporarily unavailable; a colleague will confirm", None


def _log_agent_activity(tenant_id: str, log_id: str, email: dict, result, sched_state: dict | None,
                        mailbox_id: str | None, *, escalated: str | None = None,
                        redraft: bool = False, client=None, channel: str = "email") -> None:
    """Write the curated Agent Log events for one drafted email (docs/module-agent-
    handoff.md transparency): the anchor 'drafted' event, the transport hand-off (when one
    happened), and any office appointment hold. Best-effort — db.log_agent_event swallows
    its own errors, so this never breaks drafting."""
    contact, subject = email.get("from_address"), email.get("subject")
    # 1. The anchor: a reply was drafted. Badges record what the human should notice.
    badges: dict = {}
    if escalated:
        badges["escalated"] = escalated
    if result.deferred and result.missing:
        badges["knowledge_gap"] = result.missing
    if result.scheduling_requested:
        badges["scheduling_requested"] = True
    # No sign-off configured: the draft ends however the agent chose. Said on the
    # log, in full-auto too, and never a reason to hold or block anything.
    if (result.text or "").strip() and channel == "email" and not getattr(result, "sign_off", ""):
        badges["sign_off_missing"] = True
    verb = "Re-drafted" if redraft else "Drafted"
    db.log_agent_event(
        tenant_id, "drafted",
        f"{verb} a reply to {contact or 'a customer'}" + (f" re '{subject}'" if subject else ""),
        contact=contact, subject=subject, email_log_id=log_id, mailbox_id=mailbox_id,
        detail=badges, client=client)
    bookings = (sched_state or {}).get("bookings") or []
    # 2. The transport hand-off (when the base asked the TMS desk).
    tr = (sched_state or {}).get("transport_result")
    if tr:
        summary, evd = _transport_summary(tr)
        bk = next((b for b in bookings if b.get("spaces") is not None), None)
        db.log_agent_event(
            tenant_id, "transport", summary, contact=contact, subject=subject,
            email_log_id=log_id, mailbox_id=mailbox_id,
            booking_ref=tr.get("booking_ref") or (bk or {}).get("booking_ref"),
            booking_id=(bk or {}).get("id"),
            event_date=evd or (bk or {}).get("booking_date"),
            detail=tr, client=client)
    # 2b. The field-service hand-off (when the base asked the team member desk).
    fr = (sched_state or {}).get("field_service_result")
    if fr:
        summary, evd = _field_service_summary(fr)
        bk = next((b for b in bookings if b.get("engineer_id")), None)
        db.log_agent_event(
            tenant_id, "field_service", summary, contact=contact, subject=subject,
            email_log_id=log_id, mailbox_id=mailbox_id,
            booking_ref=fr.get("booking_ref") or (bk or {}).get("booking_ref"),
            booking_id=(bk or {}).get("id"),
            event_date=evd or (bk or {}).get("booking_date"),
            detail=fr, client=client)
    # 3. Office appointment holds (people rows: spaces is None AND no team member — transport
    #    and field-service rows are already covered by their hand-off events above).
    for b in bookings:
        if b.get("spaces") is None and not b.get("engineer_id"):
            win = scheduling.window_label(b.get("window")) if b.get("window") else ""
            ref = b.get("booking_ref")
            db.log_agent_event(
                tenant_id, "office_booking",
                f"Held an appointment for {pretty_date(b.get('booking_date'))}"
                + (f" — {win}" if win and win != 'Whole day' else "")
                + (f" ({ref})" if ref else ""),
                contact=contact, subject=subject, email_log_id=log_id, mailbox_id=mailbox_id,
                booking_ref=ref, booking_id=b.get("id"), event_date=b.get("booking_date"),
                detail={"window": b.get("window")}, client=client)


def _draft_sources(retrieved: list[dict]) -> list[dict]:
    """kb.retrieve rows → the persisted provenance list [{source, chunk_id,
    similarity}]. Chunk CONTENT is deliberately not copied — the "Grounded on"
    pills fetch it live by id, so an edited chunk shows its current text."""
    out = []
    for r in retrieved or []:
        if r.get("_context"):
            continue   # read whole because the KB is small — not a match (kb.retrieve)
        try:
            sim = round(float(r.get("similarity")), 4) if r.get("similarity") is not None else None
        except (TypeError, ValueError):
            sim = None
        out.append({"source": r.get("source") or "source",
                    "chunk_id": r.get("id"), "similarity": sim})
    return out


def _trace_kb_retrieval(tenant_id: str, log_id: str, query: str,
                        draft_sources: list[dict] | None, *,
                        request_id: str | None, trigger: str, retrieval=None) -> None:
    """The draft-path retrieval trace row — closes the documented gap
    (docs/modules/module-agent-trace.md "Not (yet) traced"), mirroring the portal's
    portal_kb_retrieval shape. Skipped entirely when the KB is off (None):
    an 'empty' outcome must mean "ran and found nothing", not "never ran"."""
    if draft_sources is None:
        return
    trace.emit(tenant_id, "kb_retrieval",
               outcome=("error" if (retrieval or {}).get("state") == "failed"
                        else "ok" if draft_sources else "empty"),
               email_log_id=log_id, request_id=request_id,
               detail={"query": ((retrieval or {}).get("query", query) or "")[:300],
                       "retrieval_state": (retrieval or {}).get("state"),
                       "context_notes": (retrieval or {}).get("context", 0),
                       "results_returned": len(draft_sources),
                       "sources": [s.get("source") for s in draft_sources],
                       "trigger": trigger})


_BOOKING_REF_RX = re.compile(r"\bBK-\d+\b")


_PUNT_RX = re.compile(
    r"(?:will|'ll|going to)\s+(?:be\s+in\s+touch|get\s+back\s+to\s+you|confirm\s+(?:this\s+|it\s+|that\s+)?(?:with\s+you\s+)?(?:shortly|soon))"
    r"|colleague\s+will\s+(?:be\s+in\s+touch|confirm|contact|get\s+back)"
    r"|(?:someone|a\s+member\s+of\s+(?:the|our)\s+team)\s+will\s+(?:be\s+in\s+touch|contact|get\s+back)",
    re.I)


def _punt_while_missing(draft: str, gather_on: bool, intake_fields, booking_details) -> list[str]:
    """Required booking details still missing when the draft PUNTS instead of asking
    (docs/todo-booking-missing-fields-checklist.md fix 2). Deterministic, not prompt:
    on the gather path, a reply that says "we'll be in touch" while required fields are
    still outstanding never asked for them — a wasted email round-trip. Returns the
    missing labels (the reason the owner reads), [] when there is no punt to flag."""
    if not (draft and gather_on and intake_fields and _PUNT_RX.search(draft)):
        return []
    have = {(d.get("label") or ""): (d.get("value") or "") for d in (booking_details or [])}
    return intake.missing_required(intake_fields, have)


def _unbacked_refs(draft: str, sched_state: dict | None,
                   existing_bookings: list | None) -> list[str]:
    """Booking refs the draft cites that NOTHING backs — not a hold this run
    placed or flipped, not one of the contact's live bookings. A ref the model
    scraped from thread history (an old offer since released, a cancelled
    booking) promises the customer a slot the diary doesn't reserve — the
    double-booking door Kris named on 2026-08-31."""
    if not draft:
        return []
    backed = {b.get("booking_ref")
              for b in ((sched_state or {}).get("bookings") or [])}
    backed |= {b.get("booking_ref") for b in (existing_bookings or [])
               if b.get("status") in ("confirmed", "proposed", "offered")}
    return sorted(set(_BOOKING_REF_RX.findall(draft)) - backed)


def _after_job_context(tenant_id: str, from_address: str | None) -> str:
    """The customer's after-job tasks (src/app/after_job.py) — '' for a business
    that uses none, so its prompt is byte-identical (settings choose context)."""
    try:
        from app import after_job
        types = (db.get_scheduling_config(tenant_id) or {}).get("service_types")
        return after_job.context_lines(tenant_id, from_address, types)
    except Exception:  # noqa: BLE001 — context is best-effort, never the draft
        log.warning("after-job context failed (tenant=%s)", tenant_id, exc_info=True)
        return ""


def _documents_context(tenant_id: str, from_address: str | None, client=None) -> str:
    """The files kept on this customer's record (src/app/documents.py) — one
    line for the drafter, so 'attach the certificate' is never asked of a
    customer whose certificate is already on file."""
    if not from_address:
        return ""
    try:
        from app import documents
        return documents.context_lines(db.list_documents(tenant_id, contact_email=from_address, client=client))
    except Exception:
        return ""


def _party_context(tenant_id: str, from_address: str | None,
                   existing_bookings: list | None, client=None) -> str:
    """Job-parties awareness for the compose prompt (docs/modules/module-job-parties.md):
    who holds which role on the sender's jobs, so the draft speaks to them in
    THEIR part. Best-effort and cheap — the overwhelming no-bookings case reads
    nothing, and any failure drafts exactly as before."""
    if not existing_bookings:
        return ""
    try:
        from app import job_parties
        return job_parties.prompt_context(
            from_address, existing_bookings,
            db.job_parties_for_bookings(
                tenant_id, [b["id"] for b in existing_bookings], client=client))
    except Exception:
        log.warning("party context build failed for %s", from_address, exc_info=True)
        return ""


def _crm_context(tenant_id: str, from_address: str | None, *,
                 channel: str = "email", client=None, probe_out=None) -> str:
    """The customer-knowledge block the drafting prompt reads: who this person
    is (CRM fields, open deals + stages, open promises, recent activity), the
    dependants on their record, their package balance, certificate expiries,
    make-ups owed, their own log and any waiting-list place.

    Shared by the inbound draft AND the redraft (Kris 2026-09-21, found live on
    the rig while filming): the redraft — the thumbs-down the product presents
    as "try again, better" — built NO crm_context at all, so the second attempt
    wrote with strictly LESS knowledge than the first. It re-asked for details
    already on record and was blind to the dependants whose certificate expiry
    gates the booking.

    CRM-tier gate (docs/crm-vs-os-architecture.md §1, Julius 2026-07-24): the
    CRM unlocks at Core — a £39 email-tier tenant's draft carries no CRM
    context (there'd be nothing meaningful to read: intel is gated below too).

    Best-effort throughout: each sub-block carries its own try/except, so a
    failure in any one leaves the draft exactly as it would otherwise be.
    """
    crm_context = ""
    if not (from_address and db.module_active(tenant_id, "crm", client=client)):
        return crm_context
    try:
        # SMS threads: the from_address IS the phone number, so the CRM
        # lookup keys on the phone identity (docs/modules/module-sms-channel.md §1).
        _crm_contact = (db.crm_contact_by_phone(tenant_id, from_address, client=client)
                        if channel == "sms"
                        else db.crm_contact_by_email(tenant_id, from_address, client=client))
        if _crm_contact:
            # Practice OS (docs/os/practice.md §12.2): customer-history-aware replies.
            # ONLY for practice tenants, and only the REDACTED summary (insurer/recall/
            # last-visit + a 'has notes on file' line, NEVER raw clinical text). Best-
            # effort: any failure leaves crm_context exactly as it would be otherwise,
            # so a non-practice tenant is byte-for-byte unchanged.
            patient_summary = ""
            try:
                if db.module_active(tenant_id, "practice", client=client):
                    patient_summary = db.patient_history_summary(
                        tenant_id, _crm_contact["id"], client=client)
            except Exception:
                log.warning("patient history summary failed for %s", from_address,
                            exc_info=True)
            # Timeline awareness (2026-07-31): the drafter sees what this
            # customer recently DID — portal questions, self-serve
            # bookings/reschedules, invoices — not just what they wrote.
            _activities = []
            try:
                _activities = db.crm_activities_for_contact(
                    _crm_contact["id"], tenant_id, limit=12, client=client)
            except Exception:
                pass   # best-effort: no timeline, same draft as before
            probe_fields = []
            crm_context = crm_intel.build_crm_context(
                _crm_contact, db.crm_list_field_definitions(tenant_id, client=client),
                db.crm_list_deals(tenant_id, contact_id=_crm_contact["id"], client=client),
                db.crm_open_promises(tenant_id, contact_id=_crm_contact["id"], client=client),
                tenant_id=tenant_id, patient_summary=patient_summary,
                activities=_activities, probe_out=probe_fields)
            if probe_out is not None:
                probe_out.extend({**f, "contact_id": _crm_contact["id"]}
                                 for f in probe_fields)
            # Dependants on record (docs/modules/module-requirement-groups.md):
            # the drafter re-asks nothing shown and asks exactly the gaps.
            try:
                from app import requirement_groups as _rg
                # proof_ids so the drafter knows a `proof` gate is short a
                # certificate BEFORE it tries to book and gets refused — it can
                # ask for the card in the same reply.
                _groups = db.list_requirement_groups(tenant_id, client=client)
                _proof_keys = {}
                _docs = []
                if any(f.get("proof") for g in _groups or [] for f in g.get("fields") or []):
                    _docs = db.list_documents(tenant_id, contact_id=_crm_contact["id"], client=client, strict=True) or []
                for d in _docs:
                    if d.get("instance_id"):
                        _proof_keys.setdefault(d["instance_id"], set()).update(d.get("requirement_keys") or [])
                _known = _rg.render_known(
                    _groups,
                    db.list_requirement_instances(tenant_id, _crm_contact["id"], client=client),
                    proof_keys=_proof_keys)
                if _known:
                    crm_context = (crm_context + "\n" + _known) if crm_context else _known
            except Exception:
                pass
            # Packages (src/app/wallet.py): the balance, so the reply quotes
            # "3 of 10 hours left" instead of guessing.
            try:
                from app import wallet as _wallet
                _bal = _wallet.context_block(
                    tenant_id, _crm_contact["id"], client=client,
                    nouns=diary_config.nouns_of(db.get_scheduling_config(tenant_id, client=client)))
                if _bal:
                    crm_context = (crm_context + "\n" + _bal) if crm_context else _bal
            except Exception:
                pass
            # Certificates on contacts (slice 4): "First aid certificate:
            # lapses 12th Dec 26 (in 98 days) — a renewal is due".
            try:
                from app import crm_fields as _cf
                _cert = _cf.expiry_lines(
                    db.crm_list_field_definitions(tenant_id, entity="contact", client=client),
                    _crm_contact, datetime.now().date())
                if _cert:
                    _blk = "Certificates and dates on the customer's record:\n" + "\n".join("- " + ln for ln in _cert)
                    crm_context = (crm_context + "\n" + _blk) if crm_context else _blk
            except Exception:
                pass
            # Make-ups (src/app/makeups.py): which missed classes qualify — the
            # agent books only against this list.
            try:
                from app import makeups as _makeups
                _mk = _makeups.context_block(tenant_id, _crm_contact.get("email") or from_address,
                                             db.get_scheduling_config(tenant_id, client=client),
                                             client=client)
                if _mk:
                    crm_context = (crm_context + "\n" + _mk) if crm_context else _mk
            except Exception:
                pass
            # The customer's own log (src/app/customer_log.py): what they wrote
            # about their sessions, so "how am I getting on?" has an answer.
            try:
                from app import customer_log as _clog
                _lg = _clog.context_block(
                    tenant_id, _crm_contact.get("email") or from_address, client=client,
                    nouns=diary_config.nouns_of(db.get_scheduling_config(tenant_id, client=client)))
                if _lg:
                    crm_context = (crm_context + "\n" + _lg) if crm_context else _lg
            except Exception:
                pass
            # Waiting lists (src/app/courses.py): where they stand, or the
            # place they were offered — so "yes please" books it.
            try:
                from app import courses as _courses
                _wl = _courses.context_block(tenant_id, _crm_contact.get("email") or from_address,
                                             client=client)
                if _wl:
                    crm_context = (crm_context + "\n" + _wl) if crm_context else _wl
            except Exception:
                pass
    except Exception:
        log.warning("CRM context build failed for %s", from_address, exc_info=True)
    return crm_context


def _merge_portal_history(tenant_id: str, contact: str | None,
                          history: list[dict], client=None) -> list[dict]:
    """Interleave the contact's portal chat transcript into the replayed thread
    (Kris 2026-08-31: "that text gets used in context for the next emails").
    Each portal turn becomes a synthetic email_log-shaped row that
    agent.thread_messages already knows how to replay — a customer turn is an
    inbound with a channel prefix, an assistant turn a sent reply — merged by
    timestamp so the model reads one true chronology. Best-effort: no portal
    table / no turns / a read failure all leave the history untouched."""
    if not contact:
        return history
    try:
        turns = db.list_portal_messages(tenant_id, contact, limit=16,
                                        client=client)
    except Exception:
        log.warning("portal transcript read failed for %s", contact, exc_info=True)
        return history
    if not turns:
        return history
    synth = []
    for m in turns:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if m.get("role") == "customer":
            synth.append({"created_at": m.get("created_at") or "",
                          "kind": "inbound",
                          "status": None, "draft_reply": "",
                          "body_in": "[Said in the customer portal chat] " + content})
        elif m.get("role") == "assistant":
            synth.append({"created_at": m.get("created_at") or "",
                          "kind": "owner_message",
                          "status": "sent", "body_in": "",
                          "draft_reply": "[Portal assistant replied] " + content})
        else:
            # Retain the ambiguity for the normal verification hold below.
            synth.append({"created_at": m.get("created_at") or "",
                          "kind": None, "status": None, "draft_reply": "",
                          "body_in": content})
    return sorted(history + synth, key=lambda r: r.get("created_at") or "")


async def _read_parties(tenant_id: str, log_id: str, email: dict, from_address: str | None,
                        existing_bookings: list | None, sched_state: dict | None,
                        history: list | None, mailbox_id: str | None) -> None:
    """Job parties Phase 2 (docs/modules/module-job-parties.md): does this email NAME
    other people on a live job — "I'm the agent, the property owner John pays"?
    One cheap call, and ONLY for threads that actually have a booking. Claims
    land as source='extracted' rows — inert by D4 until confirmed on the
    diary job page. Best-effort: party reading never costs the draft. Shared
    by the first draft and the redraft (run #5, 2026-09-04: the housekeeper
    named in an email that draft suppression had filed away was only ever
    read on "Draft a reply again")."""
    try:
        # The customer's bookings AS THEY STAND after the draft — the
        # pre-draft snapshot missed the offer this very email accepted
        # (run #5, 2026-09-04: "Jo just needs the times" arrived on an
        # offered visit, the pass never ran).
        after = db.bookings_for_contact(tenant_id, from_address) if from_address else []
        thread_bookings = {b["id"]: b for b in (after or existing_bookings or [])}
        # THIS email's own jobs — what it held, accepted or moved just now.
        # The rest are the customer's other live jobs: context, but not
        # what the email is about (2026-09-04: an on-site tenant named in
        # a certificate email landed on the customer's other confirmed booking).
        own_ids = []
        for b in db.bookings_for_log(log_id, tenant_id):
            thread_bookings.setdefault(b["id"], b)
            own_ids.append(b["id"])
        for b in ((sched_state or {}).get("bookings") or []):
            if b.get("id") and b["id"] not in own_ids:
                thread_bookings.setdefault(b["id"], b)
                own_ids.append(b["id"])
        live_jobs = [b for b in thread_bookings.values()
                     if b.get("status") in party_extract.LIVE_STATUSES]
        # The rows the customer's booking list returns carry no `contact`
        # (live 2026-09-04: "booked by unknown", and the sender bound as their
        # own payer) — they are this customer's bookings, so say so.
        for b in live_jobs:
            if not b.get("contact") and from_address:
                b["contact"] = from_address
        if live_jobs and from_address:
            # A reply sent from a fresh compose starts its own provider thread;
            # the thread-scoped history then holds one email and the address
            # given earlier is out of sight. Read the contact's whole
            # conversation when the thread is thin.
            hist = list(history or [])
            if sum(1 for r in hist if direction(r) == "inbound") < 3:
                try:
                    hist = db.get_conversation(tenant_id, from_address) or hist
                except Exception:
                    pass
            claims = await party_extract.extract(
                tenant_id, live_jobs, subject=email.get("subject"),
                body=email.get("body"), from_name=email.get("from_name"),
                from_address=from_address, email_log_id=log_id,
                primary_ids=own_ids,
                history_text=party_extract.thread_history_text(hist))
            tally = party_extract.apply(tenant_id, live_jobs, claims,
                                        email_log_id=log_id,
                                        primary_ids=own_ids,
                                        sender=from_address)
            if tally["suggested"]:
                refs = ", ".join(tally["refs"]) or "this job"
                warn = (" ⚠ One claim says who PAYS — nothing re-routes "
                        "until you confirm you know them."
                        if tally["money_claims"] else "")
                db.log_agent_event(
                    tenant_id, "party_suggested",
                    f"This email names {tally['suggested']} other "
                    f"{'person' if tally['suggested'] == 1 else 'people'} on "
                    f"{refs} — confirm them on the job page.{warn}",
                    contact=from_address, subject=email.get("subject"),
                    email_log_id=log_id, mailbox_id=mailbox_id, detail=tally)
    except Exception:
        log.warning("party extraction failed for email_log %s", log_id, exc_info=True)


@actor.agent_work
async def draft_for_email(
    tenant_id: str, email: dict, source_message_id: str | None = None,
    auto_send_ok: bool = False, mailbox_id: str | None = None,
    channel: str = "email", hold_for_documents: list[str] | None = None,
    images: list | None = None,
) -> str | None:
    from_address = email.get("from_address")
    from app import inbox_rules
    auto_reason = inbox_rules.automated_reason(email, _inbox_rules(tenant_id))
    if auto_reason:
        # Automated mail by KIND (src/app/inbox_rules.py): a no-reply sender, bulk
        # mail (List-Unsubscribe / Precedence), or auto-generated mail
        # (Auto-Submitted) — never draft a reply, but LOG the message with the
        # 'Automated' label so the inbox can show it was seen and correctly left
        # alone (it lives under the Automated filter, never in the main view).
        # Deduped by (tenant, source_message_id) like any inbound; best-effort —
        # a logging failure must not change the skip.
        try:
            nr_log_id, nr_created = db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="skipped", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="automated",
                category_reason=auto_reason)
            if nr_created:
                trace.emit(tenant_id, "categorisation", email_log_id=nr_log_id,
                           contact=from_address,
                           detail={"category": "automated", "method": "rule",
                                   "trigger": "email_received",
                                   "reason": auto_reason})
        except Exception:
            log.warning("failed to log automated inbound from %s", from_address,
                        exc_info=True)
        return None
    if looks_like_payment_confirmation(from_address, email.get("subject"),
                                       email.get("body")):
        # A machine payment receipt: categorise (automated/fyi), link it to the
        # invoice it names, surface unconfirmed manual payments — never draft,
        # never spend tokens. Ahead of the ignore/suppression gates on purpose:
        # a payment landing is state the owner wants recorded regardless.
        _handle_payment_confirmation(tenant_id, email,
                                     source_message_id=source_message_id,
                                     mailbox_id=mailbox_id, channel=channel)
        return None
    if db.is_ignored(tenant_id, from_address):
        log.info("skipping draft: sender is on the ignore list")
        try:
            ignored_id, created = db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="skipped", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="fyi", category_reason="Sender is on your ignore list")
            if created:
                trace.emit(tenant_id, "categorisation", email_log_id=ignored_id,
                           contact=from_address,
                           detail={"category": "fyi", "method": "rule",
                                   "trigger": "email_received",
                                   "reason": "Sender is on your ignore list"})
        except Exception:
            log.warning("failed to log ignored inbound", exc_info=True)
        return None  # ignored sender — never auto-draft, never queue
    _self = db.mailbox_self_addresses(tenant_id, mailbox_id)
    if from_address and from_address.lower() in _self:
        # Self-loop guard: the inbound is our OWN outgoing mail re-arriving in the
        # same mailbox — from == the address this mailbox answers from OR its
        # send_as identity (2026-07-26 e2e: replies sent as hello@ routed straight
        # back and the agent booked slots against itself). Never draft a reply to
        # ourselves, and never grow a CRM contact for our own address. Sibling-
        # mailbox mail — a DIFFERENT own desk — still drafts for human review;
        # only its auto-send is held (below). Logged as Automated so the inbox
        # shows the mail was seen and deliberately left alone.
        log.info("skipping draft: our own outgoing mail re-arriving (self-loop guard)")
        try:
            sl_log_id, sl_created = db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="skipped", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="automated",
                category_reason="Our own outgoing mail re-arriving — left alone",
                kind="self_loop")
            if sl_created:
                trace.emit(tenant_id, "categorisation", email_log_id=sl_log_id,
                           contact=from_address,
                           detail={"category": "automated", "method": "rule",
                                   "trigger": "email_received",
                                   "reason": "Self-loop guard (own send-as address)"})
        except Exception:
            log.warning("failed to log self-loop inbound from %s", from_address,
                        exc_info=True)
        return None
    # Go-live hold (Kris 2026-09-01). A tenant who has just finished onboarding
    # has never seen their own inbox through this product: the history is loaded,
    # but the newsletters, suppliers and no-reply robots in it are not marked as
    # ignored yet. Drafting from the first minute spends tokens answering mail
    # that was never going to be answered, and greets the owner with a pile of
    # drafts they didn't want.
    #
    # So a new tenant starts held. The mail still arrives, threads and files —
    # this uses the same shape as the suppression gate below, so it shows in the
    # inbox and can be read and triaged — it simply is not drafted. The owner
    # ignores whom they like and then releases drafting themselves, which is the
    # last step of onboarding and the first rung of the sending ladder.
    #
    # Sits AFTER the ignore and self-loop gates and BEFORE any model call.
    if _cfg_draft_hold(tenant_id):
        try:
            held_log_id, _ = db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="skipped", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="needs_reply",
                category_reason="Drafting is off while you set up — turn it on in "
                                "Settings when you've finished going through your inbox")
        except Exception:
            log.warning("failed to log held inbound from %s", from_address, exc_info=True)
            return None
        # Held mail still belongs in the CRM (Julius 2026-09-03: a whole held
        # period's senders were invisible — no contacts, no timeline). The
        # ZERO-LLM ingest runs here — contact + activity from data already in
        # hand, not a token spent; the LLM passes (fields, deals, promises,
        # sentiment) run when a draft eventually does (the redraft path now
        # backfills them).
        try:
            crm.ingest_email_log_row(tenant_id, {
                "id": held_log_id, "from_address": from_address,
                "from_name": email.get("from_name"),
                "subject": email.get("subject"), "status": "skipped"})
        except Exception:
            log.warning("CRM ingest failed for held inbound %s", held_log_id,
                        exc_info=True)
        return None
    # Draft suppression (docs/modules/module-draft-suppression.md): the owner has let 3+
    # consecutive drafts to this sender go unanswered — stop spending tokens on
    # them. The inbound still lands in the inbox under FYI; an email that looks
    # unusual for this sender (urgency keywords / 3x their average length) breaks through
    # with a one-off draft instead.
    _sup = suppression.check_inbound(tenant_id, from_address, email.get("subject"),
                                     email.get("body"), mailbox_id=mailbox_id)
    if _sup["action"] == "suppress":
        _streak = int((_sup.get("stats") or {}).get("consecutive_unsent_drafts") or 0)
        try:
            db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="skipped", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="fyi",
                category_reason=f"Drafting paused — {_streak} drafts to this "
                                "contact went unanswered")
        except Exception:
            log.warning("failed to log suppressed inbound from %s", from_address,
                        exc_info=True)
        return None
    reengaged = _sup["action"] == "reengage"
    tenant = db.get_tenant(tenant_id) or {}
    # Trial gate: stop BEFORE spending model tokens once the free trial is over
    # and the access gate is on (gate_allows returns True instantly when the gate
    # is off — the launch state — so this path is a no-op there). An automatic
    # caller (webhook/poller) just stops drafting; a human-initiated draft raises
    # so the dashboard can route the user to the billing CTA.
    if not trial.gate_allows(tenant):
        if auto_send_ok:
            log.info("trial expired for tenant %s — auto-draft skipped", tenant_id)
            return None
        raise trial.TrialLimitReached(trial.evaluate(tenant))
    # Document hold (Kris 2026-09-22: "avoid a wasted draft — hold the draft
    # until the right point"). An email carrying a card — a professional's certificate
    # record, a certificate — is about what the card SAYS, and the card is read
    # only once it is kept. Drafting first wrote a reply that could not know the
    # new date and was thrown away the moment it did. So the row files with NO
    # draft, the caller keeps and reads the files, and `after_document_read`
    # either drafts then — once, against the updated record — or holds until
    # the owner confirms what the card says (a "You confirm" field).
    if hold_for_documents:
        try:
            held_id, created = db.insert_email_log(
                tenant_id, from_address, email.get("subject"), email.get("body"), "",
                status="pending", source_message_id=source_message_id,
                thread_id=email.get("thread_id"), mailbox_id=mailbox_id,
                from_name=email.get("from_name"), channel=channel,
                category="needs_reply", thread_state="needs_attention",
                action_summary=DOC_HOLD_READING,
                category_reason=f"Reading {', '.join(hold_for_documents[:3])} before "
                                "replying — the reply is written once BLAM8 knows what it says")
        except Exception:
            log.warning("failed to file document-held inbound from %s", from_address, exc_info=True)
            return None
        if not created:
            return None          # a twin delivery already filed (and is reading) it
        try:
            # The contact must exist before the file is kept: the document and
            # the chip both key on it. Zero-LLM, as for the go-live hold.
            crm.ingest_email_log_row(tenant_id, {
                "id": held_id, "from_address": from_address,
                "from_name": email.get("from_name"),
                "subject": email.get("subject"), "status": "pending"})
        except Exception:
            log.warning("CRM ingest failed for document-held inbound %s", held_id, exc_info=True)
        # Tell the open pages it arrived (K1 take 2026-09-22: the held email
        # sat in the database while Today and the thread showed nothing until
        # a reload — every other path announces itself, this one didn't).
        _announce_held(tenant_id, held_id, from_address, mailbox_id)
        return held_id
    # Live inbox ("instant chat"): the inbound exists in no table until the insert
    # below, so the typing indicator's whole lifetime is this in-memory window. The
    # finally guarantees the bubble clears even when drafting dies mid-flight.
    _draft_key = source_message_id or f"{from_address}|{email.get('subject') or ''}"
    # Idempotency claim (see _drafts_in_flight above): if a twin caller is
    # already drafting this exact inbound, walk away before spending a token —
    # the winner's row is the one row this message was ever going to get.
    # Atomic without a lock: every caller shares one event loop and nothing
    # awaits between the check and the add.
    _claim = (tenant_id, _draft_key)
    if _claim in _drafts_in_flight:
        log.info("skipping draft: same message already being drafted (%s)", _draft_key)
        return None
    # ...and the same question across PROCESSES, which the set above cannot see
    # (2026-09-22): a duplicate webhook delivery lands in a different worker,
    # both drafted, and the one that finished first won the insert — once with
    # a placeholder reply. db.claim_inbound_draft fails open, so a claim table that
    # cannot be reached costs a duplicate draft, never a missing one.
    # Claimed on BOTH the provider id and a content fingerprint. The id alone
    # was not enough (2026-09-22: two runs drafted one email and the id they
    # each carried evidently differed), and the fingerprint is identical
    # however the message arrives. Either being taken means someone else has
    # it. Both are released together in the finally.
    _claim_keys = [_draft_key]
    _fp = db.draft_fingerprint(from_address, email.get("subject"), email.get("body"))
    if _fp != _draft_key:
        _claim_keys.append(_fp)
    _got: list[str] = []
    for _k in _claim_keys:
        if db.claim_inbound_draft(tenant_id, _k):
            _got.append(_k)
            continue
        log.info("skipping draft: another process holds the claim (%s)", _k)
        for _done in _got:                  # never strand the ones we did take
            db.release_inbound_draft(tenant_id, _done)
        return None
    _drafts_in_flight.add(_claim)
    # Keep the inbound readable before its row exists, including text beyond
    # the old 600-character preview. The thread scrolls long customer messages.
    events.begin_drafting(tenant_id, _draft_key, {
        "from_address": from_address, "from_name": email.get("from_name"),
        "subject": email.get("subject"), "mailbox_id": mailbox_id,
        "body": agent.strip_quoted(email.get("body") or "")})
    try:
        cfg, booking_cfg = _draft_settings(tenant_id, mailbox_id)
        cfg["industry"] = tenant.get("industry")
        query = f"{email.get('subject', '')}\n{email.get('body', '')}"
        # Keep the retrieval rows, not just the flattened prompt text — their
        # {source, chunk_id, similarity} become the draft's "Grounded on" pills
        # (docs/modules/module-concierge-transparency.md §B). None = KB off; [] = ran
        # and matched nothing (the honest "inferred, not from your data" state).
        # Escalation is known BEFORE any model call (keyword match on the inbound
        # + config) — an escalation hold spends nothing (Kris 2026-08-31): no
        # retrieval, no draft, and no banner smuggled into reply text a tap away
        # from a customer. The row queues empty; the human writes this one.
        # Matched on the QUOTE-STRIPPED body: a reply quoting a thread that once
        # said "refund" must not escalate forever (live e2e catch, 2026-08-31).
        escalated = agent.escalation_match(
            {"subject": email.get("subject"),
             "body": agent.strip_quoted(email.get("body") or "")}, cfg)
        if escalated:
            retrieved, knowledge, draft_sources = [], "", None
        else:
            retrieved = await kb.retrieve(tenant_id, query, mailbox_id=mailbox_id, whole_if_small=True)
            knowledge = kb.render_for_prompt(retrieved)
            draft_sources = _draft_sources(retrieved) if kb.is_configured() else None
            # Live ticker: the search happened — say so on the open thread.
            if draft_sources is not None:
                events.drafting_step(
                    tenant_id, from_address,
                    working.knowledge_line(working.retrieval_record(retrieved, query, True)))
        lookup_record = working.retrieval_record(retrieved, query, draft_sources is not None)
        # Conversation memory (Phase 4/5): replay the prior thread. Scope to the provider
        # thread (Phase 5) when we have one, so a genuinely new conversation starts fresh;
        # else fall back to all of this contact's mail. The current inbound isn't stored
        # yet, so every existing row is prior context.
        thread_id = email.get("thread_id")
        history = db.get_conversation(tenant_id, from_address, thread_id=thread_id) if (from_address or thread_id) else []
        # Portal awareness (Kris 2026-08-31): the same customer's portal chat —
        # what they asked and what the assistant told them — interleaves into
        # the replayed thread, so an email after a portal cancellation request
        # is answered with that conversation in mind.
        history = _merge_portal_history(tenant_id, from_address, history)
        try:
            history = replayable_history(history)
        except VerificationUnavailable as exc:
            return _hold_unreadable_settings(
                tenant_id, email, mailbox_id, exc, source_message_id=source_message_id, channel=channel)
        # Booking-aware threads: the bookings this contact already holds, so a follow-up
        # reply references their existing booking (ref/date/status) instead of re-offering.
        existing_bookings = db.bookings_for_contact(tenant_id, from_address) if from_address else []
        probe_fields = []
        party_context, crm_context, storefront_context, credit_refusal = _customer_draft_context(
            tenant_id, email, existing_bookings, channel=channel, probe_out=probe_fields)
        # Scheduling / transport hand-off module gate: when the tenant's subscriptions
        # rows say so, the draft call gains the office diary tools and/or the ask_transport
        # hand-off (D3/D9 — gated by data, never code paths). The composite executor records
        # any hold placed (office or via the TMS specialist) so it can be linked/resolved below.
        tools, executor, sched_state, diary, flags = _scheduling_setup(
            tenant_id, from_address, thread_id, mailbox_id,
            existing_bookings=existing_bookings, email=email, history=history, booking_cfg=booking_cfg)
        if escalated:
            # The stub result carries everything downstream needs — needs_reply,
            # needs_attention, and the WHY the attention bubble reads. No tokens.
            events.drafting_step(tenant_id, from_address,
                                 f"Escalation keyword matched: “{escalated}” — held for you")
            result = agent.DraftResult(
                text="", usage={}, model="",
                thread_state="needs_attention", category="needs_reply",
                category_reason=(f"Escalation keyword matched: “{escalated}” — "
                                 "this reply is yours to write"))
        else:
            # The allowance gate (Kris 2026-09-01). Asked BEFORE the model is, so
            # a refusal costs nothing, and it stops ONLY the drafting: the mail
            # is still filed, the thread still updates, the diary and CRM carry
            # on. The thread lands as needs_attention with the reason on it —
            # the same shape the escalation stub above uses, because "a human
            # has to write this one" is the same outcome either way.
            #
            # Inert until usage_enforcement is switched to "hard"; under soft it
            # always allows and never spends a tenant's prepaid credit.
            claim = usage.claim_draft(tenant_id)
            if not claim["allowed"]:
                events.drafting_step(
                    tenant_id, from_address,
                    "Monthly email allowance used up — nothing drafted")
                result = agent.DraftResult(
                    text="", usage={}, model="",
                    thread_state="needs_attention", category="needs_reply",
                    category_reason=("Your monthly email allowance is used up, so the "
                                     "agent didn't draft this one — top up to start it "
                                     "writing again."))
            else:
                # NOT "Writing the reply…" (Kris 2026-09-21): nothing is written
                # yet — the tool calls come first, and agent.draft_reply publishes
                # the writing line at the turn where it becomes true.
                events.drafting_step(tenant_id, from_address, "Reading the email…")
                await _ground_extract(tenant_id, sched_state, email)
                result = await _draft_or_outage(tenant_id, from_address, cfg, email, knowledge=knowledge, history=history,
                                                 tools=tools, tool_executor=executor, diary_summary=diary,
                                                 scheduling=flags["scheduling"], transport=flags["transport"],
                                                 no_dates=flags.get("no_dates", False),
                                                 other_dates=flags.get("other_dates", "hold"),
                                                 intake=flags.get("intake", ""),
                                                 gather_intake=flags.get("gather_intake", False),
                                                 field_service=flags.get("field_service", False),
                                                 existing_bookings=existing_bookings,
                                                 crm_context=crm_context, channel=channel,
                                                 storefront=flags.get("storefront", False),
                                                 storefront_context=storefront_context,
                                                 tenant_id=tenant_id,
                                                 party_context=party_context,
                                                 images=images,
                                                 on_step=lambda label: events.drafting_step(
                                                     tenant_id, from_address, label))
        result = _verification_hold(result, sched_state)
        _merge_handoff_usage(result, sched_state)
        # The model writes in Markdown; emails go out as plain text, so normalise before we
        # store it — the approval UI and the sent email then both read cleanly (Fix 2).
        draft = textfmt.to_plain_text(result.text)
        # Knowledge hold (Kris 2026-08-31): the model deferred a missing business
        # fact and — by rule 5 — wrote NO reply, saving the tokens a throwaway
        # holding draft would have burned. The row queues with the gap named;
        # adding the knowledge (or Draft anyway) triggers the real draft.
        # Held even when the model half-complied and wrote a reply anyway, like the
        # system block below (live 2026-09-30: it deferred whether the business does
        # an unlisted piece of work, and its price, but still wrote a reply that
        # invented why it couldn't be priced and implied the work is done).
        knowledge_hold = result.deferred and bool(result.missing)
        if knowledge_hold and draft.strip():
            log.info("knowledge hold: dropped a reply written around a deferred fact (%s)", result.missing)
            draft = ""
        # System-constraint hold (rule 5b, Julius 2026-09-02 — "the draft
        # shouldn't have been written; an error should appear with the
        # solution, like a Gap"): the tools refused for a configuration reason
        # the agent couldn't work around. NO draft is stored even if the model
        # half-complied and wrote one — a reply written around the wall
        # promises what the system won't do (the live case leaked the model's
        # deliberation instead). The thread queues with the constraint named.
        system_block = bool(result.blocked)
        if system_block:
            draft = ""
        # Escalation hold: deliberately empty (no banner in the reply text —
        # the reason lives in category_reason for the attention bubble).
        esc_hold = bool(escalated) and not draft.strip()
        # Say-vs-do on booking refs (Kris 2026-08-31, live: a reply claimed
        # "pencilled in... BK-1047" while this run placed NO hold and that ref
        # was a cancelled row quoted from thread history — the promised slot
        # was not reserved, an open door to a double booking). Ground truth
        # wins: any ref in the draft that neither this run's holds nor the
        # contact's live bookings back flags the thread and blocks auto-send.
        ghost_refs = _unbacked_refs(draft, sched_state, existing_bookings)
        # Punt-while-missing: on the gather path a "we'll be in touch" reply with required
        # details outstanding never asked for them — flag it, name what's missing.
        # (Settings are only read when a punt phrase is actually present.)
        punt_missing = (_punt_while_missing(
            draft, True,
            (db.get_scheduling_config(tenant_id) or {}).get("intake_fields"),
            result.booking_details)
            if flags.get("gather_intake") and _PUNT_RX.search(draft or "") else [])
        # Drafting failed outright (empty reply even after agent.py's corrective
        # retry): queue the thread loudly with NO draft text — a human writes or
        # hits Redraft. Never store the model's raw output as if it were a reply.
        draft_failed = (not draft.strip() and not knowledge_hold and not esc_hold
                        and not system_block)
        knowledge_gap = result.missing if result.deferred else None
        # Scheduling/module gap: the customer asked to book but no diary module is
        # active (tools is None). Surfaced to the human as an upgrade signal. Gated on
        # tools is None so it can never fire for a tenant that actually has scheduling.
        scheduling_gap = bool(result.scheduling_requested and tools is None)
        # Inbox-at-a-glance state/outcome (agent.py rule 6). The model self-reports the
        # state, but the ground-truth signals computed OUTSIDE it win: an escalation keyword
        # match or a booking asked-for with no diary module both mean a human must act
        # (needs_attention); a hold actually placed this run means booked. Escalation takes
        # precedence (it still needs a human even if a hold was placed).
        if escalated or scheduling_gap or draft_failed or knowledge_hold \
                or system_block or ghost_refs or punt_missing:
            thread_state = "needs_attention"
        elif sched_state and sched_state.get("bookings"):
            thread_state = "booked"
        else:
            thread_state = result.thread_state
        # Smart inbox label (auto-categorisation): the pipeline owns the transitions;
        # the model only classified the inbound (needs_reply | fyi | automated). An
        # informational inbound never enters the approval queue — its draft is kept
        # for the record but the row lands 'skipped' (no pending badge, no push, no
        # auto-send; a wrong call is one Redraft away from a normal queue entry).
        category, informational = _classify(
            result.category, escalated=bool(escalated), scheduling_gap=scheduling_gap,
            deferred=result.deferred, thread_state=thread_state,
            # Offered slots are options awaiting the customer's pick, not a hold —
            # an offers-only draft stays draft_ready, not booking_pending.
            held=bool(sched_state and any(
                b.get("status") != "offered" for b in sched_state.get("bookings") or [])))
        if reengaged:
            # Re-engagement one-off (docs/modules/module-draft-suppression.md §7): the whole
            # point of breaking suppression is that the owner SEES this draft — an
            # fyi/automated call must not file it away unqueued.
            informational = False
            if category in ("fyi", "automated"):
                category = "needs_reply"
        if draft_failed or knowledge_hold or system_block or ghost_refs or punt_missing:
            # A failed draft must land in the queue where a human sees it — a
            # model that failed to write a reply also can't be trusted to have
            # classified the thread as safely ignorable. A knowledge hold needs
            # the human just the same: the gap is theirs to fill. A system
            # block is the owner's settings to fix. A ghost ref means the
            # reply promises a booking the diary doesn't hold.
            informational, category = False, "needs_reply"
        log_id, created = db.insert_email_log(
            tenant_id, from_address, email.get("subject"), email.get("body"), draft,
            status="skipped" if informational else "pending",
            knowledge_gap=knowledge_gap, scheduling_gap=scheduling_gap,
            source_message_id=source_message_id, thread_id=thread_id, mailbox_id=mailbox_id,
            thread_state=thread_state, request_summary=result.request_summary,
            action_summary=(result.action_summary if (sched_state or {}).get("verification_unavailable")
                            else "Draft failed — write a reply or hit Redraft"
                            if draft_failed
                            else "No draft written — missing knowledge"
                            if knowledge_hold
                            else "No draft written — waiting on the customer"
                            if system_block and _gate_gap(sched_state)
                            else "No draft written — a setting blocks this request"
                            if system_block
                            else "No draft written — escalation matched"
                            if esc_hold else result.action_summary),
            searched_for=lookup_record.get("query") or None,
            booking_details=list(result.booking_details),
            from_name=email.get("from_name"),
            contact_name=result.contact_name, contact_company=result.contact_company,
            channel=channel,
            category=category,
            category_reason=(result.category_reason if (sched_state or {}).get("verification_unavailable")
                             else "Unusual email from a paused contact — drafted a reply "
                             "just in case" if reengaged
                             else f"The reply mentions {', '.join(ghost_refs)} but the "
                                  "diary holds no such booking — nothing is reserved; "
                                  "check the draft before sending" if ghost_refs
                             else "The reply says we'll be in touch but never asked for: "
                                  f"{', '.join(punt_missing)} — ask for them before sending"
                                  if punt_missing
                             # The system-constraint hold's message IS the ghost:
                             # the constraint named, and where the fix lives.
                             # Unless the diary actually refused for a MISSING
                             # DETAIL (Kris 2026-09-21) — then nothing is wrong
                             # with the settings, the customer simply has not
                             # sent something, and sending the owner to Diary
                             # settings is a wild goose chase.
                             else f"Waiting on the customer: {_gate_gap(sched_state)}"
                             if system_block and _gate_gap(sched_state)
                             else f"A setting blocks this request: {result.blocked} "
                                  "— fix it in Diary settings, then hit Redraft"
                             if system_block
                             else result.category_reason or None),
            draft_sources=draft_sources,
            draft_reasoning=_working_panel(sched_state, result, lookup_record,
                                           draft=draft, needs_owner=thread_state == "needs_attention"),
        )
        # Count queued probe instructions once, never duplicate deliveries or
        # empty/unqueued replies. The model may omit the suggested question.
        if created and probe_fields and draft.strip() and not informational:
            try:
                probe = probe_fields[0]
                crm_fields.log_probe_attempt(tenant_id, probe["contact_id"],
                                             probe["id"], draft_id=log_id)
            except Exception:
                log.warning("probe attempt log failed for %s", from_address, exc_info=True)
    except VerificationUnavailable as exc:
        return _hold_unreadable_settings(
            tenant_id, email, mailbox_id, exc, source_message_id=source_message_id, channel=channel)
    finally:
        _drafts_in_flight.discard(_claim)
        for _k in _claim_keys:
            db.release_inbound_draft(tenant_id, _k)
        events.end_drafting(tenant_id, _draft_key)
    events.publish(tenant_id, {"type": "draft_ready", "contact": from_address,
                               "mailbox_id": mailbox_id, "log_id": log_id,
                               "thread_state": thread_state, "category": category})
    if created and not informational and draft and thread_id:
        # A newer draft in the same thread answers the conversation as it now
        # stands — an older reply still waiting answers a message the customer
        # has since moved past (Kris 2026-09-27: Dev's round-1 options draft sat
        # "pending" under his round-3 booking, forever). Retire it. Holds are
        # left alone: this draft's scheduling already adopted or released them.
        db.retire_superseded_drafts(tenant_id, thread_id, keep_id=log_id)
    if created and not informational and not reengaged:
        # Draft suppression bookkeeping: one more draft the owner hasn't sent yet
        # (a successful auto-send below resets the streak straight back — net zero
        # for full-auto tenants). A re-engagement one-off never extends the streak.
        suppression.record_draft_generated(tenant_id, from_address,
                                           subject=email.get("subject"),
                                           mailbox_id=mailbox_id)
    if created:
        # The classification decision, with its why: which label, whether the
        # model's call survived or a pipeline ground-truth signal overrode it
        # (method 'rule'), and the draft's request_id → its llm_calls rows.
        trace.emit(tenant_id, "categorisation", email_log_id=log_id,
                   contact=from_address, request_id=result.request_id,
                   detail={"category": category,
                           "model_category": result.category,
                           "method": "llm" if category == result.category else "rule",
                           "reason": result.category_reason or None,
                           "trigger": "draft_generated",
                           "informational": informational})
        _trace_kb_retrieval(tenant_id, log_id, query, draft_sources,
                            request_id=result.request_id, trigger="draft_generated", retrieval=lookup_record)

    def _why_held() -> str:
        """Why this draft is waiting, said on the push/Today card (full-auto test
        2026-09-26: every hold read the same 'Draft ready')."""
        try:
            return _why_held_inner()
        except Exception:
            return ""

    def _why_held_inner() -> str:
        if escalated:
            return f"'{escalated}' is on your escalate list"
        if sentiment_hot:
            return "they sound upset"
        if getattr(result, "owner_check", ""):
            return f"your rule: {result.owner_check}"
        if getattr(result, "deferred", False) and getattr(result, "missing", ""):
            return f"couldn't answer: {result.missing}"
        if getattr(result, "thread_state", "") == "needs_attention":
            return "needs your attention" + (f" — {result.category_reason}"
                                             if getattr(result, "category_reason", "") else "")
        return ""

    def _urgency_notes() -> None:
        """The Today flags that are about the JOB, not the reply — so they fire
        whether the reply waits or went out on its own (full-auto test
        2026-09-26: Lucy's dialysis and the out-of-hours question vanished on an
        auto-sent reply)."""
        if not created:
            return
        try:
            from app import urgency
            body = agent.strip_quoted(email.get("body") or "")
            name = result.contact_name or email.get("from_name")
            if sched_state and sched_state.get("ooh_withheld"):
                urgency.ask_owner_ooh(tenant_id, contact=from_address, name=name,
                                      withheld=sched_state["ooh_withheld"])
            urgency.maybe_flag(
                tenant_id, contact=from_address, name=name, body=body,
                service_types=(db.get_scheduling_config(tenant_id) or {}).get("service_types"))
            urgency.alert_flag(tenant_id, contact=from_address, name=name, body=body,
                               phrases=cfg.get("alert_words"))
        except Exception:
            log.warning("urgency notes failed for email_log %s", log_id, exc_info=True)

    def _push_draft_ready(reason: str = "") -> None:
        # Device push (PWA): "Draft ready — tap to review". Fired ONLY where the
        # draft actually lands in the approval queue — a successfully auto-sent
        # reply gives the owner nothing to do, and a duplicate webhook delivery
        # (created=False) must not re-notify. Fire-and-forget, never raises.
        # …and only while it is STILL waiting (run log F18, services take
        # 2026-09-25): this runs near the end of the pipeline, after the draft
        # is already on the thread, and Kris approved Sarah's in the 19 s
        # between — the send settled a card that did not exist yet, then this
        # wrote it, and Today said "Draft ready" for a sent reply. Checked
        # before writing, and again after in case the approve lands in between.
        def _waiting() -> bool:
            try:
                return (db.get_email_log_entry(log_id, tenant_id) or {}).get("status") == "pending"
            except Exception:
                return True       # unsure: tell the owner rather than stay quiet
        _urgency_notes()
        if not _waiting():
            return
        try:
            push.notify_draft_ready(
                tenant_id,
                contact_name=result.contact_name or email.get("from_name"),
                contact_address=from_address, subject=email.get("subject"),
                log_id=log_id, reason=reason or _why_held())
        except Exception:
            log.warning("draft-ready push failed for email_log %s", log_id, exc_info=True)
        if not _waiting():
            db._settle_draft_ready_card(log_id, tenant_id)

    # Count one trial email — only for a genuinely NEW draft (a duplicate webhook
    # delivery is created=False) of a tenant still on the free trial, and only
    # while the gate is on (so the flag-off path stays free of extra DB calls).
    if created and settings.require_subscription_gate and trial.on_trial(tenant):
        trial.consume(tenant_id)
    held = sched_state["bookings"] if sched_state else []
    if held:
        if created:
            db.link_bookings_to_log([b["id"] for b in held], log_id, tenant_id)
        else:
            # Duplicate webhook delivery — the ORIGINAL draft's holds stand. Release
            # this run's FRESH holds, or one email would hold capacity twice. But a
            # row this run merely FLIPPED (the customer accepting an offered slot —
            # accepted_flip) is the customer's real booking, shared state that
            # predates this run: leave it standing and stamp it onto the canonical
            # log so approve/skip still resolves it. (Live 2026-08-31: a duplicate
            # run cancelled the freshly-accepted BK-1048 as its own leftover while
            # the surviving reply told the customer the slot was theirs.)
            flips = [b["id"] for b in held if b.get("accepted_flip")]
            if flips:
                db.link_bookings_to_log(flips, log_id, tenant_id)
            for b in held:
                if b["id"] not in flips:
                    db.set_booking_status(b["id"], "cancelled", tenant_id)
    # Storefront reservations (transactions) placed this run. On a duplicate webhook the
    # original draft's reservation stands, so release this run's held stock — otherwise one
    # email would reserve the same goods twice.
    reservations = (sched_state or {}).get("reservations") or []
    if reservations and not created:
        for tx in reservations:
            try:
                db.shop_release_reservation(tx["id"], tenant_id)
            except Exception:
                log.warning("failed to release duplicate storefront reservation %s",
                            tx.get("id"), exc_info=True)
    # Agent Log (transparency): record what the agent did — only for a genuinely new
    # draft, so a duplicate webhook delivery never double-logs.
    if created:
        _log_agent_activity(tenant_id, log_id, email, result, sched_state, mailbox_id,
                            escalated=escalated, channel=channel)
        # CRM ingest (docs/architecture/CRM_design.md): fold the inbound into companies/contacts/
        # activities. Best-effort — the CRM must never cost a tenant their draft.
        # Tier split (docs/crm-vs-os-architecture.md §1, Julius 2026-07-24): the
        # zero-LLM ingest below runs for EVERY tenant (a few DB writes riding data
        # we already log — so the CRM is pre-filled the day a £39 tenant upgrades),
        # but the LLM passes (field extraction + deal/promise/sentiment intel) run
        # only with the 'crm' module on: the email tier never pays CRM AI costs.
        # Consequence: below Core there is no sentiment hold — escalation keywords
        # (tenant_config, matched in agent.escalation_match) remain that tier's guard.
        sentiment_hot = False   # set by intel below; holds full-auto send
        try:
            ingested = crm.ingest_email_log_row(tenant_id, {
                "id": log_id, "from_address": from_address,
                "from_name": email.get("from_name"),
                "contact_name": result.contact_name,
                "contact_company": result.contact_company,
                "subject": email.get("subject"),
                "status": "skipped" if informational else "pending",
                "request_summary": result.request_summary,
            })
            crm_llm_on = db.module_active(tenant_id, "crm")
            # Phase 2 — custom-field autofill: one cheap extraction call, ONLY
            # when this tenant defined fields (everyone else skips at the list
            # lookup) and the email produced a CRM record to write onto. The
            # confidence gate (crm_fields) decides autofill / suggest / drop.
            if crm_llm_on and (ingested.get("contact_id") or ingested.get("company_id")):
                defs = db.crm_list_field_definitions(tenant_id)
                if not defs:
                    from app import dependants_extract
                    await dependants_extract.run_for_email(
                        tenant_id, ingested.get("contact_id"), subject=email.get("subject"),
                        body=email.get("body"), from_name=email.get("from_name"), email_log_id=log_id)
                if defs:
                    # Built-ins (the sender's phone → the first-class column)
                    # ride the SAME call — tenants with no definitions of
                    # their own still pay nothing.
                    defs = defs + crm_fields.BUILTIN_DEFINITIONS
                    extracted = await crm_extract.extract(
                        tenant_id, defs, subject=email.get("subject"),
                        body=email.get("body"), from_name=email.get("from_name"),
                        email_log_id=log_id)
                    tally = crm_fields.apply_extraction(
                        tenant_id,
                        {"contact": ingested.get("contact_id"),
                         "company": ingested.get("company_id")},
                        defs, extracted, log_id)
                    if tally["autofilled"] or tally["suggested"]:
                        db.log_agent_event(
                            tenant_id, "crm_autofill",
                            f"Read {tally['autofilled']} CRM field value(s) from this "
                            f"email" + (f" ({tally['suggested']} more suggested for "
                                        "your confirmation)" if tally["suggested"] else ""),
                            contact=from_address, subject=email.get("subject"),
                            email_log_id=log_id, mailbox_id=mailbox_id, detail=tally)
                    # Dependants, read not typed (module-requirement-groups.md
                    # Phase 2): its own small call, only for tenants with a group.
                    from app import dependants_extract
                    await dependants_extract.run_for_email(
                        tenant_id, ingested.get("contact_id"), subject=email.get("subject"),
                        body=email.get("body"), from_name=email.get("from_name"), email_log_id=log_id)
            # Remembered booking requirements (app/intake_memory, 2026-09-21): a
            # first-time customer had no record when the hold gate ran — it is
            # born just above — so what they gave on a remembered row lands now;
            # and one Agent Log line covers everything this email kept on the
            # record. Best-effort under the same umbrella as the ingest.
            if isinstance(sched_state, dict) and (
                    sched_state.get("remember_pending") or sched_state.get("remembered_labels")):
                from app import intake_memory
                intake_memory.flush_pending(
                    tenant_id, ingested.get("contact_id"), sched_state,
                    fields=(db.get_scheduling_config(tenant_id) or {}).get("intake_fields"),
                    email_log_id=log_id, contact=from_address, subject=email.get("subject"),
                    mailbox_id=mailbox_id)
            # Phase 3 — deal clustering + promise tracking (on-write, design
            # doc paragraph 14): does this email belong to a deal, start one, or
            # promise anything? Same best-effort umbrella as the ingest above.
            if crm_llm_on and ingested.get("contact_id"):
                intel = await crm_intel.process_email(
                    tenant_id, direction="inbound", subject=email.get("subject"),
                    body=email.get("body"), contact_id=ingested["contact_id"],
                    company_id=ingested.get("company_id"),
                    contact_name=result.contact_name or email.get("from_name"),
                    company_name=result.contact_company, email_log_id=log_id)
                # Sentiment escalation: a customer reading frustrated/angry is a
                # judgment call the owner should see — full-auto tenants get the
                # draft held for approval (gate below), and the Agent Log says why.
                sent_state = intel.get("sentiment") or {}
                if sent_state.get("temperature") == "hot":
                    sentiment_hot = True
                    db.log_agent_event(
                        tenant_id, "sentiment",
                        f"This customer reads {sent_state.get('mood')}"
                        + (f" — {sent_state['reason']}" if sent_state.get("reason") else "")
                        + ". Flagged for your attention.",
                        contact=from_address, subject=email.get("subject"),
                        email_log_id=log_id, mailbox_id=mailbox_id, detail=sent_state)
                promise_tally = intel.get("promises") or {}
                has_promise_activity = any(promise_tally.get(k) for k in
                                           ("created", "reinforced", "resolved"))
                if intel.get("deal_id") or has_promise_activity:
                    db.log_agent_event(
                        tenant_id, "crm_intel",
                        "Filed this email onto a deal" if intel.get("deal_id")
                        else "Noted commitments from this email",
                        contact=from_address, subject=email.get("subject"),
                        email_log_id=log_id, mailbox_id=mailbox_id, detail=intel)
        except Exception:
            log.warning("CRM ingest failed for email_log %s", log_id, exc_info=True)
        # Job parties Phase 2 (docs/modules/module-job-parties.md): does this email NAME
        # other people on a live job — "I'm the agent, the property owner John pays"?
        # One cheap call, and ONLY for threads that actually have a booking
        # (everyone else pays nothing). Claims land as source='extracted' rows —
        # inert by D4 until confirmed on the diary job page. Best-effort: party
        # reading never costs the draft.
        await _read_parties(tenant_id, log_id, email, from_address, existing_bookings,
                            sched_state, history, mailbox_id)
    if result.model:   # an escalation hold made no call — nothing to meter
        usage.record_anthropic(tenant_id, result.model, result.usage, operation="draft", email_log_id=log_id)
    # Quotes Phase 3 (docs/modules/module-quotes.md): the drafter just priced this
    # enquiry (holds carrying quoted_amount) → draft the quote DOC too. Zero
    # extra LLM — the pricing already happened — and best-effort inside: a
    # quote failure never costs the reply. `created` guards webhook dupes.
    if created and not informational:
        quote_autodraft.maybe_draft_for_log(tenant_id, cfg, log_id, from_address)
    # Full-auto send — opt-in on two axes: the effective auto_send (tenant_config,
    # overridable per mailbox — orders@ can run full-auto while complaints@ stays
    # draft-only) AND an automatic-path caller that set auto_send_ok (only the automatic
    # inbound paths do; human-initiated dashboard drafts never send). Held back for
    # escalation matches (always a human) and for duplicate webhook deliveries (`created`
    # is False when we deduped to an existing row, so we never double-send). Knowledge-gap
    # deferrals DO send — the reply is honest. A send failure leaves the row pending,
    # where it surfaces as a normal draft to approve.
    # Also held back when the inbound is from ANY of the tenant's own mailboxes (the
    # SIBLING SET): auto-replying to yourself re-arrives in your inbox and would loop
    # forever — and with several mailboxes, orders@ and info@ could loop each other.
    # And held back when the wholesale credit gate fired (docs/os/wholesale.md §12b):
    # a credit refusal is a judgment call the owner should see once — it queues as a
    # pending draft even for full-auto tenants.
    # An informational (fyi/automated) inbound never auto-sends and never pushes —
    # the whole point of the label is that nobody needs to act on it.
    if informational:
        return log_id
    # A hold or failure has no reply to send — full-auto tenants wait like
    # everyone else until the knowledge lands or a human writes.
    if created and auto_send_ok and cfg.get("auto_send") and not escalated \
            and not ghost_refs and not punt_missing and from_address and draft.strip():
        if credit_refusal:
            log.info("auto-send held for email_log %s: wholesale credit gate fired — "
                     "refusal drafts always wait for the owner", log_id)
            _push_draft_ready()
            return log_id
        if sentiment_hot:
            # The emotional moments are the ones the AI hands to the human: a
            # frustrated/angry customer never gets a fully automatic reply — the
            # draft waits in the approval queue even for full-auto tenants.
            log.info("auto-send held for email_log %s: customer sentiment is running "
                     "hot — the reply waits for the owner", log_id)
            _push_draft_ready()
            return log_id
        if from_address.lower() in db.own_mailbox_addresses(tenant_id):
            log.info("auto-send held for email_log %s: inbound is from one of the tenant's own mailboxes", log_id)
            _push_draft_ready()
            return log_id
        # The owner's rules and the agent's own "this needs you" (full-auto test
        # 2026-09-26): a £6,000 refit under "check with Dan over £1,000" went out
        # promising Dan would call; a question the knowledge couldn't answer went
        # out with invented "we don't do that". Each now waits, saying why.
        why = (f"your rule: {result.owner_check}" if getattr(result, "owner_check", "") else
               f"couldn't answer: {result.missing}" if (getattr(result, "deferred", False)
                                                         and getattr(result, "missing", "")) else
               "needs your attention" if getattr(result, "thread_state", "") == "needs_attention"
               else "")
        if why:
            log.info("auto-send held for email_log %s: %s", log_id, why)
            _push_draft_ready(why)
            return log_id
        # Quotes Phase 3 'auto' mode: the accept link rides the reply itself
        # (email only — SMS stays terse), flipping the drafted quote to sent.
        # "" in every other mode/state, so the concatenation is always safe.
        draft_out = draft
        from app import deposits
        dep_inv, dep_block = None, ""
        if channel == "email":
            draft_out += quote_autodraft.link_for_send(tenant_id, cfg, log_id)
            if held:
                # Deposit on confirmation (src/app/deposits.py): the auto-sent
                # confirmation carries the deposit invoice for the holds it
                # confirms — staged now, finalised after the send, voided if
                # the send fails.
                dep_inv, dep_block = deposits.stage_for_log(tenant_id, log_id, from_address)
                draft_out += dep_block
        # EU AI Act Art 50(1): an auto-sent reply is the agent interacting directly
        # with the recipient, so it discloses itself — see src/app/disclosure.py. The
        # stored draft stays clean; only the outbound copy gains the footer.
        body_out = disclosure.wrap(cfg, draft_out, channel=channel, kind="reply")
        if channel == "email":
            # Customer portal: the reference-code sign-off rides the same
            # wrap-at-send convention (src/app/portal.py — "" when portal off).
            from app import portal
            body_out += portal.email_footer(tenant_id, cfg, from_address)
        try:
            # The send dispatches on channel (docs/modules/module-sms-channel.md §4): an SMS
            # thread leaves through the tenant's provisioned number, email through
            # the mailbox. sms.send_sms re-checks the opt-out list (compliance gate).
            if channel == "sms":
                await sms.send_sms(tenant_id, from_address, body_out, email_log_id=log_id)
            else:
                await mailbox.send_reply(
                    tenant_id, from_address, email.get("subject"), body_out,
                    source_message_id=source_message_id, mailbox_id=mailbox_id,
                )
            db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
            suppression.record_sent(tenant_id, from_address)  # engagement: streak resets
            _urgency_notes()
            if getattr(result, "rule_applied", "") and _is_never_rule(result.rule_applied,
                                                                       cfg.get("never_do")):
                try:
                    from app import urgency
                    urgency.rule_note(tenant_id, contact=from_address,
                                      name=result.contact_name or email.get("from_name"),
                                      rule=result.rule_applied,
                                      what=getattr(result, "request_summary", ""))
                except Exception:
                    log.warning("rule note failed for email_log %s", log_id, exc_info=True)
            trace.emit(tenant_id, "label_transition", email_log_id=log_id,
                       contact=from_address,
                       detail={"to_category": "quote_sent",
                               "trigger": "reply_sent", "via": "auto"})
            events.publish(tenant_id, {"type": "draft_sent", "contact": from_address,
                                       "mailbox_id": mailbox_id, "log_id": log_id})
            db.log_agent_event(tenant_id, "sent",
                               f"Sent the reply to {from_address} (auto)",
                               contact=from_address, subject=email.get("subject"),
                               email_log_id=log_id, mailbox_id=mailbox_id,
                               detail={"via": "auto"})
            try:  # CRM: the reply that just went out joins the activity feed,
                  # and our own commitments in it become we_owe promises.
                crm.note_outbound_sent(tenant_id, log_id)
                await crm_intel.note_outbound(tenant_id, log_id)
            except Exception:
                log.warning("CRM outbound ingest failed for email_log %s", log_id, exc_info=True)
            if held:
                # Full-auto D4: a successful send IS the approval — confirm the hold
                # and put it in the tenant's calendar (best-effort).
                if db.resolve_holds_for_log(log_id, "confirmed", tenant_id):
                    deposits.after_send(tenant_id, dep_inv, log_id=log_id, customer_email=from_address)
                    trace.emit(tenant_id, "label_transition", email_log_id=log_id,
                               contact=from_address,
                               detail={"to_category": "booking_pending",
                                       "trigger": "booking_created", "via": "auto"})
                    try:  # CRM: the auto-confirmed booking joins the timeline,
                          # and any promise it fulfils ticks off (2026-09-03).
                        confirmed = [_b for _b in db.bookings_for_log(log_id, tenant_id)
                                     if _b.get("status") == "confirmed"]
                        for _b in confirmed:
                            crm.note_booking_activity(tenant_id, _b)
                        if confirmed:
                            await crm_intel.resolve_from_action(
                                tenant_id, from_address,
                                "Confirmed booking(s): " + "; ".join(
                                    f"{_b.get('booking_ref') or 'booking'} on "
                                    f"{_b.get('booking_date')} — {_b.get('summary') or ''}"
                                    for _b in confirmed),
                                ref=("booking", confirmed[0].get("id"),
                                     confirmed[0].get("booking_ref")))
                    except Exception:
                        log.warning("CRM booking activity failed for email_log %s",
                                    log_id, exc_info=True)
                await calendar_sync.push_bookings(tenant_id, email_log_id=log_id)
        except sms.SmsOptedOut:
            # A visible skip, never a silent drop (docs/modules/module-sms-channel.md §4):
            # the recipient texted STOP after this draft was queued.
            db.set_email_log_status(log_id, "skipped", tenant_id)
            log.info("auto-send skipped for email_log %s: recipient has opted out", log_id)
        except Exception:
            # Holds stay proposed: the row is still pending, so approve confirms and
            # skip releases them later; the expiry sweep is the backstop.
            deposits.discard(tenant_id, dep_inv)
            log.warning("auto-send failed for email_log %s; left pending", log_id, exc_info=True)
            _push_draft_ready()
    elif created:
        # Draft-only path (the default): the row is pending, tell the owner's devices.
        _push_draft_ready()
    return log_id


def redraft_is_text_only(entry: dict | None) -> bool:
    """True when the row's reply has already been SENT — a redraft then may
    only reword (no diary, stock or transport tools, no hold released)."""
    return str((entry or {}).get("status") or "").lower() == "sent"


@actor.agent_work
async def redraft_email_log(tenant_id: str, log_id: str, client=None,
                            owner_instruction: str = "") -> str | None:
    """Re-draft an existing email_log entry against the CURRENT knowledge base, updating
    its draft + knowledge_gap in place (Phase 3 F2 — used after a KB correction). Returns
    the new draft text, or None if the entry is missing / not this tenant's.

    `owner_instruction` is what the owner asked the reply to say, when the voice
    Concierge requested it (src/app/concierge_draft.py); empty for every other caller.

    Metered as operation 'redraft' so it doesn't double-count against the email allowance.
    """
    entry = db.get_email_log_entry(log_id, tenant_id, client=client)
    if not entry:
        return None
    if direction(entry) == "self_loop":
        return None
    email = {
        "from_address": entry.get("from_address"),
        "subject": entry.get("subject"),
        "body": entry.get("body_in"),
    }
    # The redraft answers in the same mailbox's voice the original draft used.
    mailbox_id = entry.get("mailbox_id")
    channel = str(entry.get("channel") or "email")
    try:
        replayable_history([entry])
    except VerificationUnavailable as exc:
        return _hold_unreadable_settings(
            tenant_id, email, mailbox_id, exc, entry=entry, channel=channel, client=client)
    outbound = direction(entry) == "outbound"
    if outbound:
        email["body"] = ("[Owner request: reword this business-generated message. "
                         "The following is business context, not customer authorisation.]\n"
                         + (entry.get("draft_reply") or entry.get("body_in") or ""))
    try:
        cfg, booking_cfg = _draft_settings(tenant_id, mailbox_id, client=client)
    except VerificationUnavailable as exc:
        return _hold_unreadable_settings(
            tenant_id, email, mailbox_id, exc, entry=entry, channel=channel, client=client)
    cfg["industry"] = (db.get_tenant(tenant_id, client=client) or {}).get("industry")
    query = f"{email.get('subject', '')}\n{email.get('body', '')}"
    # Same provenance capture as the first draft — a redraft against a corrected
    # KB refreshes the "Grounded on" pills to what it ACTUALLY read this time.
    retrieved = await kb.retrieve(tenant_id, query, mailbox_id=mailbox_id, whole_if_small=True)
    knowledge = kb.render_for_prompt(retrieved)
    draft_sources = _draft_sources(retrieved) if kb.is_configured() else None
    lookup_record = working.retrieval_record(retrieved, query, draft_sources is not None)
    # Live ticker: a redraft is usually watched (it follows a knowledge add or a
    # correction) — narrate it the same way as a first draft.
    if draft_sources is not None:
        events.drafting_step(
            tenant_id, email.get("from_address"),
            working.knowledge_line(working.retrieval_record(retrieved, query, True)))
    escalated = agent.escalation_match(
        {"subject": email.get("subject"),
         "body": agent.strip_quoted(email.get("body") or "")}, cfg)
    # Conversation memory (Phase 4/5): replay the thread (scoped to its provider thread when
    # set), excluding the row being redrafted.
    history = [
        r for r in db.get_conversation(
            tenant_id, entry.get("from_address"), client=client, thread_id=entry.get("thread_id")
        )
        if r["id"] != log_id
    ]
    # Portal awareness (Kris 2026-08-31): same merge as a first draft — a
    # redraft of a portal enquiry sees the portal conversation behind it.
    history = _merge_portal_history(tenant_id, entry.get("from_address"), history,
                                    client=client)
    try:
        history = replayable_history(history)
    except VerificationUnavailable as exc:
        return _hold_unreadable_settings(
            tenant_id, email, mailbox_id, exc, entry=entry, channel=channel, client=client)
    # Scheduling / transport (D4): a re-draft releases the old proposed hold FIRST —
    # before the diary tooling is even built — so the fresh draft books against true
    # capacity and can place its own (office hold and/or the TMS specialist's).
    # A reply that already went out is a fact the customer holds: its redraft
    # (fix-a-draft after a knowledge correction) may reword, never re-book.
    # With the diary tools on, the redraft re-held the visit the sent reply had
    # already confirmed (live 2026-09-04: BK-1005 beside BK-1001).
    already_sent = redraft_is_text_only(entry) or outbound
    if not already_sent and {"scheduling", "tms", "fsm"} & db.active_modules(tenant_id):
        db.resolve_holds_for_log(log_id, "cancelled", tenant_id, client=client)
    # Booking-aware (after the old hold was released above, so it isn't included);
    # the executor's live-diary gate reads the same list the prompt does.
    existing_bookings = db.bookings_for_contact(tenant_id, entry.get("from_address"), client=client)
    if already_sent:
        # Even assembling write-capable tools can create a contact. Sent replies
        # keep their read-only facts, without constructing any action executor.
        tools, executor, sched_state, diary = None, None, None, None
        flags = {"scheduling": False, "transport": False, "gather_intake": False,
                 "field_service": False, "storefront": False}
    else:
        tools, executor, sched_state, diary, flags = _scheduling_setup(
            tenant_id, entry.get("from_address"), entry.get("thread_id"), mailbox_id,
            client=client, existing_bookings=existing_bookings, email=email, history=history,
            # The owner's own instruction authorises an out-of-hours offer.
            owner_directed=bool((owner_instruction or "").strip()), booking_cfg=booking_cfg)
    party_context, crm_context, storefront_context, _ = _customer_draft_context(
        tenant_id, email, existing_bookings, channel=channel, client=client)
    events.drafting_step(tenant_id, email.get("from_address"), "Reading the email…")
    await _ground_extract(tenant_id, sched_state, email)
    result = await _draft_or_outage(tenant_id, email.get("from_address"), cfg, email, knowledge=knowledge, history=history,
                                     tools=tools, tool_executor=executor, diary_summary=diary,
                                     scheduling=flags["scheduling"], transport=flags["transport"],
                                     no_dates=flags.get("no_dates", False),
                                     other_dates=flags.get("other_dates", "hold"),
                                     intake=flags.get("intake", ""),
                                     gather_intake=flags.get("gather_intake", False),
                                     field_service=flags.get("field_service", False),
                                     existing_bookings=existing_bookings,
                                     storefront=flags.get("storefront", False),
                                     crm_context=crm_context, channel=channel,
                                     storefront_context=storefront_context,
                                     tenant_id=tenant_id, log_operation="redraft",
                                     party_context=party_context,
                                     owner_instruction=owner_instruction,
                                     images=_stored_photos(tenant_id, log_id, entry.get("from_address")))
    result = _verification_hold(result, sched_state)
    _merge_handoff_usage(result, sched_state)
    # The model writes in Markdown; emails go out as plain text, so normalise before we
    # store it — the approval UI and the sent email then both read cleanly (Fix 2).
    draft = textfmt.to_plain_text(result.text)
    # No escalation banner in the text (Kris 2026-08-31): a redraft is an
    # explicit human request, so it DOES draft — but the reply stays clean and
    # the needs_reply classification still routes it through the human.
    gap = result.missing if result.deferred else None
    # Same say-vs-do ref guard as a first draft: a redraft citing a booking
    # nothing backs flags the thread rather than promising a ghost slot.
    ghost_refs = _unbacked_refs(draft, sched_state, existing_bookings)
    # Drafting failed outright (empty even after agent.py's corrective retry —
    # run #8, 2026-09-05: a redraft whose structured output came back as
    # placeholders was stored as an empty draft_ready row, "yours to write",
    # with no reason). Filed exactly as the inbound path files it: loudly,
    # with no draft text and the reason on the thread. A knowledge hold or a
    # system block is a deliberate empty reply, not a failure.
    draft_failed = (not draft.strip() and not gap
                    and not getattr(result, "blocked", None))
    # Refresh the inbox state/outcome too (rule 6) — a re-draft after a KB correction can
    # genuinely change the situation (e.g. a gap is now answered). Escalation still forces
    # needs_attention; the scheduling-gap override is a draft-time signal not recomputed here.
    thread_state = ("needs_attention" if escalated or ghost_refs or draft_failed
                    else result.thread_state)
    # Smart inbox label: a redraft is human-initiated — the owner WANTS a reply —
    # so an fyi/automated call is never honoured here (no queue suppression); the
    # row keeps its normal actionable label.
    if escalated or result.deferred or thread_state == "needs_attention":
        category = "needs_reply"
    elif sched_state and any(b.get("status") != "offered"
                             for b in sched_state.get("bookings") or []):
        category = "booking_pending"
    else:
        category = "draft_ready"
    # Identity is omit-to-keep: a redraft that learned the name refreshes it, one
    # that didn't never blanks what an earlier draft established.
    identity = {k: v for k, v in (("contact_name", result.contact_name),
                                  ("contact_company", result.contact_company)) if v}
    db.update_email_log_draft(log_id, draft, tenant_id, client=client, knowledge_gap=gap,
                              thread_state=thread_state, request_summary=result.request_summary,
                              action_summary=("Draft failed — write a reply or hit Redraft"
                                              if draft_failed else result.action_summary),
                              searched_for=lookup_record.get("query") or None,
                              booking_details=list(result.booking_details),
                              category=category,
                              category_reason=(f"The reply mentions {', '.join(ghost_refs)} "
                                               "but the diary holds no such booking — "
                                               "nothing is reserved; check the draft before "
                                               "sending" if ghost_refs
                                               else result.category_reason or "The agent couldn't write this one — "
                                               "its reply came back empty twice. Hit Draft "
                                               "again, or write the reply yourself"
                                               if draft_failed
                                               else result.category_reason or None),
                              draft_sources=draft_sources,
                              draft_reasoning=_working_panel(sched_state, result, lookup_record,
                                           draft=draft, needs_owner=thread_state == "needs_attention"), **identity)
    if sched_state and sched_state["bookings"]:
        db.link_bookings_to_log([b["id"] for b in sched_state["bookings"]], log_id, tenant_id)
    trace.emit(tenant_id, "categorisation", email_log_id=log_id,
               contact=email.get("from_address"), request_id=result.request_id,
               detail={"category": category,
                       "model_category": result.category,
                       "method": "llm" if category == result.category else "rule",
                       "reason": result.category_reason or None,
                       "trigger": "draft_regenerated"})
    _trace_kb_retrieval(tenant_id, log_id, query, draft_sources,
                        request_id=result.request_id, trigger="draft_regenerated", retrieval=lookup_record)
    _log_agent_activity(tenant_id, log_id, email, result, sched_state, mailbox_id,
                        escalated=escalated, redraft=True, client=client, channel=channel)
    usage.record_anthropic(tenant_id, result.model, result.usage, operation="redraft", email_log_id=log_id)
    if outbound:
        return draft  # Rewording business text supplies no new customer facts.
    # The holds this redraft placed replace the ones released above, and the
    # quote auto-drafted from the old holds was withdrawn with them: draft a
    # fresh one from the new holds, under the same setting and conditions as a
    # first draft (round three, 1 Oct 2026 - F26). A reply already sent re-books
    # nothing, so it re-quotes nothing. Best-effort inside, like the first draft.
    if not already_sent:
        quote_autodraft.maybe_draft_for_log(
            tenant_id, cfg, log_id, entry.get("from_address"), client=client)
    # Job parties on a redraft too (run #5): the email that names the
    # housekeeper may be the one suppression filed and the owner redrafted.
    await _read_parties(tenant_id, log_id, email, entry.get("from_address"), existing_bookings,
                        sched_state, history, mailbox_id)
    # CRM enrichment (Julius 2026-09-03): a redraft can be the FIRST real pass
    # over a thread the born-held gate filed without one — run the same ingest
    # + LLM passes as a first draft so contact, custom fields, deals and
    # promises catch up. Safe on ordinary redrafts too: the activity feed
    # dedupes on source_id, field autofill re-merges the same values, and the
    # intel pass is promise-aware (reinforce, never re-create). Best-effort —
    # a CRM hiccup never costs the redraft.
    try:
        ingested = crm.ingest_email_log_row(tenant_id, {
            "id": log_id, "from_address": email.get("from_address"),
            "from_name": entry.get("from_name"),
            "contact_name": result.contact_name,
            "contact_company": result.contact_company,
            "subject": email.get("subject"),
            "status": entry.get("status") or "pending",
            "request_summary": result.request_summary,
        })
        if db.module_active(tenant_id, "crm") and ingested.get("contact_id"):
            defs = db.crm_list_field_definitions(tenant_id)
            if defs:
                defs = defs + crm_fields.BUILTIN_DEFINITIONS
                extracted = await crm_extract.extract(
                    tenant_id, defs, subject=email.get("subject"),
                    body=email.get("body"), from_name=entry.get("from_name"),
                    email_log_id=log_id)
                crm_fields.apply_extraction(
                    tenant_id, {"contact": ingested.get("contact_id"),
                                "company": ingested.get("company_id")},
                    defs, extracted, log_id)
            from app import dependants_extract
            await dependants_extract.run_for_email(
                tenant_id, ingested.get("contact_id"), subject=email.get("subject"),
                body=email.get("body"), from_name=entry.get("from_name"), email_log_id=log_id)
            await crm_intel.process_email(
                tenant_id, direction="inbound", subject=email.get("subject"),
                body=email.get("body"), contact_id=ingested["contact_id"],
                company_id=ingested.get("company_id"),
                contact_name=result.contact_name or entry.get("from_name"),
                company_name=result.contact_company, email_log_id=log_id)
    except Exception:
        log.warning("CRM enrichment failed on redraft %s", log_id, exc_info=True)
    return draft


MODEL_OUTAGE_REASON = ("The AI couldn't be reached when this email came in, so no reply "
                       "was written — hit Redraft, or write the reply yourself.")


async def _draft_or_outage(_tid: str, _who: str, cfg: dict, email: dict, /, **kw):
    """agent.draft_reply, but a model OUTAGE (the API refusing every call —
    live 2026-09-26: the account's credit ran out mid-draft) becomes a failed
    draft instead of an exception. The exception aborted the pipeline before
    the email row was written: the owner saw a drafting spinner that vanished
    on open, "Opening…" on the inbox row for ever, and no email at all. Now it
    queues like any failed draft — the email visible, the reason said,
    Redraft one tap away. Anything that isn't the API failing still raises."""
    import anthropic
    try:
        return await agent.draft_reply(cfg, email, **kw)
    except anthropic.APIError as e:
        log.error("draft: model unavailable (tenant=%s): %s", _tid, e)
        events.drafting_step(_tid, _who, "The AI couldn't be reached — nothing drafted")
        return agent.DraftResult(text="", usage={}, model="", thread_state="needs_attention",
                                 category="needs_reply", category_reason=MODEL_OUTAGE_REASON)


def _is_never_rule(named: str | None, never_do: str | None) -> bool:
    """Is the rule the draft says it applied one of the owner's NEVER rules?
    (Live 2026-09-26: the model also named knowledge notes and routine Always
    steps, and Today filled with "your rule applied".) Most of the named rule's
    words must be in one Never line."""
    import re as _re
    words = set(_re.findall(r"[a-z]{4,}", (named or "").lower()))
    if not words:
        return False
    for line in (never_do or "").splitlines():
        have = set(_re.findall(r"[a-z]{4,}", line.lower()))
        if have and len(words & have) >= max(2, round(len(words) * 0.6)):
            return True
    return False


def _stored_photos(tenant_id: str, log_id: str, contact: str | None) -> list | None:
    """Photos kept from this email, for a redraft to see (src/app/photos.py)."""
    try:
        from app import photos
        return photos.stored_for_log(tenant_id, log_id, contact) or None
    except Exception:
        log.warning("stored photos read failed (%s)", log_id, exc_info=True)
        return None


@actor.agent_work
async def draft_for_message(
    tenant_id: str, message_id: str, auto_send_ok: bool = False, mailbox_id: str | None = None,
    *, provider_message: dict | None = None,
) -> str | None:
    # Idempotent: Microsoft Graph delivers change notifications at-least-once (and retries
    # when the ack is slow — e.g. a free-tier cold start), so skip an inbound message we've
    # already drafted instead of duplicating it. The unique index is the race backstop.
    # Provider message ids are mailbox-scoped, so one email CC'd to two connected
    # mailboxes arrives as two DIFFERENT ids → two drafts, one per mailbox (by design:
    # each persona answers its own inbox).
    existing = db.get_email_log_by_message(tenant_id, message_id)
    if existing:
        return existing["id"]
    msg = provider_message if provider_message is not None else await mailbox.get_message(
        tenant_id, message_id, mailbox_id=mailbox_id)
    if not msg:
        return None
    email = {
        "from_address": (msg.get("from") or {}).get("address"),
        "from_name": (msg.get("from") or {}).get("name"),
        "subject": msg.get("subject"),
        "body": msg.get("body"),
        "thread_id": msg.get("thread_id"),
        "headers": msg.get("headers") or {},     # the inbox rules read these
    }
    # A card to read first (document hold, above): the reply waits for it.
    cards = card_names(msg.get("attachments"))
    hold = cards if cards and _reads_documents(tenant_id) else None
    # Photos go to the drafting model (src/app/photos.py, Kris 2026-09-26): fetched
    # now, before the reply is written, so a photographed error code is read.
    try:
        from app import photos
        images = await photos.fetch_for_draft(tenant_id, message_id, msg.get("attachments"),
                                              mailbox_id=mailbox_id)
    except Exception:
        log.warning("photo fetch for the draft failed (%s)", message_id, exc_info=True)
        images = []
    log_id = await draft_for_email(tenant_id, email, source_message_id=message_id,
                                   auto_send_ok=auto_send_ok, mailbox_id=mailbox_id,
                                   hold_for_documents=hold, images=images or None)
    read = await keep_attachments(tenant_id, message_id, log_id,
                                  {**msg, "from_address": email.get("from_address")},
                                  mailbox_id=mailbox_id)
    if hold and log_id:
        await after_document_read(tenant_id, log_id, read)
    return log_id


@actor.agent_work
async def draft_for_latest(tenant_id: str) -> str | None:
    email = await mailbox.fetch_latest_email(tenant_id)
    if not email:
        return None
    mid = email.get("id")
    if mid:  # don't re-draft the latest message if it's already in the queue
        existing = db.get_email_log_by_message(tenant_id, mid)
        if existing:
            return existing["id"]
    log_id = await draft_for_email(tenant_id, email, source_message_id=mid)
    await keep_attachments(tenant_id, mid, log_id, email, mailbox_id=None)
    return log_id


# ── the document hold (Kris 2026-09-22) ──────────────────────────────────────
# The states a held row's action_summary moves through. They are also the
# markers: a row is document-held exactly while it reads one of the first two,
# and a release CLAIMS the row by moving it off them (db.claim_email_log_summary),
# so two confirms landing together write ONE reply.
DOC_HOLD_READING = "No draft yet — reading the attached document first"
DOC_HOLD_CONFIRM = "No draft yet — waiting for you to confirm what the document says"
DOC_HOLD_WRITING = "Writing the reply now the document is read"


def _announce_held(tenant_id: str, log_id: str | None, contact: str | None,
                   mailbox_id: str | None = None) -> None:
    """The live-inbox event every other path sends when a row lands or moves,
    so a held email shows (and then shows what it waits on) without a reload."""
    try:
        events.publish(tenant_id, {"type": "draft_ready", "contact": contact,
                                   "mailbox_id": mailbox_id, "log_id": log_id,
                                   "thread_state": "needs_attention", "category": "needs_reply"})
    except Exception:  # noqa: BLE001
        pass


def card_names(attachments) -> list[str]:
    """The attachments worth reading before replying: PDFs, not inline images.
    A photographed card has no text layer to read, so it never holds a reply."""
    out = []
    for a in attachments or []:
        a = a or {}
        name = str(a.get("name") or "").strip()
        ctype = str(a.get("content_type") or "").lower()
        if name and not a.get("inline") and (ctype == "application/pdf"
                                             or name.lower().endswith(".pdf")):
            out.append(name)
    return out


def _reads_documents(tenant_id: str) -> bool:
    """Only a business that tracks dependants holds a reply for a card — there
    the card can change what the reply may say. Everyone else drafts as before
    (their files are still kept, labelled and read, just after the draft)."""
    try:
        return bool(db.list_requirement_groups(tenant_id))
    except Exception:  # noqa: BLE001
        return False


async def _write_held_reply(tenant_id: str, log_id: str, entry: dict | None = None) -> None:
    # The open thread shows it working, as any other draft does (Kris K1 take
    # 2026-09-22: "it did draft, it just didn't show any progress" — the AI
    # writing bubble and the ghost's live steps). No subject/body in the event:
    # the customer's email is already on the thread, so live.js must not put a
    # provisional copy of it up as well — only the typing bubble and its steps.
    entry = entry or db.get_email_log_entry(log_id, tenant_id) or {}
    key = f"held:{log_id}"
    events.begin_drafting(tenant_id, key, {
        "from_address": entry.get("from_address"), "from_name": entry.get("from_name"),
        "subject": "", "body": "", "mailbox_id": entry.get("mailbox_id")})
    try:
        events.drafting_step(tenant_id, entry.get("from_address"), "Document saved — preparing a reply…")
        await redraft_email_log(tenant_id, log_id)
    finally:
        events.end_drafting(tenant_id, key)
    try:
        events.publish(tenant_id, {"type": "draft_ready", "log_id": log_id,
                                   "contact": entry.get("from_address")})
    except Exception:  # noqa: BLE001
        pass


async def after_document_read(tenant_id: str, log_id: str, read: dict | None) -> None:
    """The card is read: write the reply now, once, against the updated record
    — unless the read left facts for the OWNER to confirm ("You confirm"), in
    which case the row says so and waits for `release_document_hold`. A read
    that failed or found nothing writes the reply anyway: holding forever for a
    card nobody could read would be the worse failure. Best-effort."""
    try:
        entry = db.get_email_log_entry(log_id, tenant_id)
        if not entry or entry.get("action_summary") != DOC_HOLD_READING:
            return
        waiting = db.pending_chips_for_email(tenant_id, log_id)
        if waiting:
            db.update_email_log_draft(
                log_id, "", tenant_id, action_summary=DOC_HOLD_CONFIRM,
                thread_state="needs_attention",
                category_reason=f"Waiting on you: confirm {_chip_words(tenant_id, waiting)} "
                                f"from the document {entry.get('from_name') or 'the customer'} "
                                "sent — the reply is written as soon as you do")
            _announce_held(tenant_id, log_id, entry.get("from_address"), entry.get("mailbox_id"))
            return
        if db.claim_email_log_summary(log_id, tenant_id, DOC_HOLD_READING, DOC_HOLD_WRITING):
            await _write_held_reply(tenant_id, log_id, entry)
    except Exception:  # noqa: BLE001
        log.warning("after-document-read failed for %s", log_id, exc_info=True)


async def release_document_hold(tenant_id: str, log_id: str | None) -> None:
    """A chip from a held email was confirmed or dismissed: once none are left
    pending, the reply is written — against the record as the owner left it.
    Called detached from the confirm button. Best-effort."""
    if not log_id:
        return
    try:
        if db.pending_chips_for_email(tenant_id, log_id):
            return
        if db.claim_email_log_summary(log_id, tenant_id, DOC_HOLD_CONFIRM, DOC_HOLD_WRITING):
            await _write_held_reply(tenant_id, log_id, db.get_email_log_entry(log_id, tenant_id))
    except Exception:  # noqa: BLE001
        log.warning("document-hold release failed for %s", log_id, exc_info=True)


def _chip_words(tenant_id: str, chips: list[dict]) -> str:
    """The owner's title and date (e.g. "Alfie's certificate, 22 Sep 27") for the row."""
    try:
        from app import requirement_groups as rg
        from app.dates import pretty_date
        groups = rg.clean_groups(db.list_requirement_groups(tenant_id))
        parts = []
        for c in chips:
            i = db.get_requirement_instance(c.get("record_id"), tenant_id) or {}
            g = next((x for x in groups if x.get("id") == i.get("group_id")), None)
            f = rg.field_for(g, c.get("field_key")) if g else None
            label = (f or {}).get("label") or c.get("field_key")
            val = str(c.get("value") or "")
            d = rg.parse_date(val)
            if d:
                val = pretty_date(d.isoformat())
            parts.append(f"{i['name']}'s {label} ({val})" if i.get("name") else f"{label} ({val})")
        return ", ".join(parts) or "what it says"
    except Exception:  # noqa: BLE001
        return "what it says"


async def keep_attachments(tenant_id: str, message_id: str | None, log_id: str | None,
                           msg: dict, *, mailbox_id: str | None) -> dict | None:
    """Documents phase 1 (Kris 2026-09-04): the files themselves, kept on the
    sender's record once the row exists. Best-effort — never the draft's
    problem. Shared by the push path and the poll path. Returns what reading
    them produced (dependants_extract.read_documents) — None when nothing was
    read — which the document hold decides on."""
    if not (log_id and message_id and (msg or {}).get("attachments")):
        return None
    try:
        from app import documents
        from_address = (msg.get("from_address") or ((msg.get("from") or {}).get("address")
                                                    if isinstance(msg.get("from"), dict) else None))
        kept = await documents.save_email_attachments(
            tenant_id, mailbox_id=mailbox_id, message_id=message_id, email_log_id=log_id,
            from_address=from_address,
            attachments=msg.get("attachments"),
            # The words around the file, so a card named generically still
            # reaches the right dependant when the email names it.
            text_hint=f"{msg.get('subject') or ''} {msg.get('body') or ''}"[:2000])
    except Exception:
        log.warning("keeping attachments failed for %s", message_id, exc_info=True)
        return None
    # Read what the files SAY (slice 4, 2026-09-22): label each one and take the
    # facts it states — a certificate's new expiry — onto the record, through the
    # same confidence gate and "You confirm" setting as a date typed in an email.
    readable = [d for d in (kept or []) if d.get("text")]
    if not readable:
        return None
    from app import dependants_extract
    return await dependants_extract.read_documents(
        tenant_id, next((d.get("contact_id") for d in readable if d.get("contact_id")), None),
        readable, email_log_id=log_id, contact=from_address)
