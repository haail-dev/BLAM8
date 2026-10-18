"""Invoicing — group delivered jobs by customer into invoices (TMS baseline).

Pure: delivered bookings + the rate card in, draft invoices (grouped by the
customer the booking already names — bookings.contact, the schema's "cross-module
identity seed") out. Each line is a booking at its stored quote, or recomputed
from the rate card when it was never quoted. VAT at 20%. The route mints refs and
persists (db.create_invoice); this module shapes the grouping.
"""
from app import db
from app.pricing import DEFAULT_RATE, miles_between, quote

VAT_RATE = 0.20


def _preview_tax(tenant_id: str, client=None) -> tuple[float, bool]:
    """(VAT rate as a fraction, prices include VAT) from the tenant's invoice settings
    — so the generate-preview matches what create_invoice will actually bill (VAT can be
    toggled off, re-rated, or the stated prices can already include it)."""
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    return invoice_config.effective_vat_rate(s) / 100.0, s.get("prices_include_vat") is True


def _amount_for(booking: dict, rate_card: dict | None, coord_idx: dict) -> float:
    """A booking's charge: its stored quote if it has one, else recomputed from
    the rate card (so an unquoted-but-delivered job still invoices honestly)."""
    stored = booking.get("quoted_amount")
    if stored is not None:
        return round(float(stored), 2)
    coll = (booking.get("collection_postcode") or "").upper() or None
    deliv = (booking.get("delivery_postcode") or "").upper() or None
    miles = miles_between(coll, deliv, coord_idx)
    return quote(rate_card, booking.get("spaces") or 0, miles)["total"]


def _group(out_groups: dict, vat_rate: float, include: bool = False) -> list[dict]:
    """Finalise grouped lines into draft invoices (subtotal/VAT/total), biggest first.
    `vat_rate` is a fraction (0.20) and `include` whether the stated prices already
    include VAT, so the PREVIEW matches what create_invoice will bill under the
    tenant's invoice settings (the split is invoice_config.split_vat's)."""
    from app import invoice_config
    cfg = {"show_vat": True, "vat_rate": round(vat_rate * 100.0, 6)}
    out = []
    for g in out_groups.values():
        subtotal, vat, total = invoice_config.split_vat(
            sum(l["amount"] for l in g["lines"]), cfg, include=include)
        out.append({**g, "subtotal": subtotal, "vat": vat,
                    "total": total, "count": len(g["lines"])})
    out.sort(key=lambda d: -d["total"])
    return out


def drafts(completed_bookings: list[dict], rate_card: dict | None,
           coord_idx: dict | None = None, vat_rate: float = VAT_RATE,
           include: bool = False) -> list[dict]:
    """Group delivered (transport) bookings by customer into draft invoices,
    biggest first. Each draft = {customer_email, customer_name, lines[], subtotal,
    vat, total, count}."""
    coord_idx = coord_idx or {}
    groups: dict = {}
    for b in completed_bookings:
        if b.get("spaces") is None:
            continue
        key = (b.get("contact") or "").lower() or "—"
        g = groups.setdefault(key, {"customer_email": b.get("contact"),
                                    "customer_name": b.get("contact_name") or b.get("contact") or "Unknown customer",
                                    "lines": []})
        g["lines"].append({
            "booking_id": b["id"],
            "description": f"{b.get('booking_ref') or b['id'][:8]} · {(b.get('summary') or '')[:50]}",
            "amount": _amount_for(b, rate_card, coord_idx),
            "date": str(b.get("booking_date"))[:10],
        })
    return _group(groups, vat_rate, include)


def _apply_party_overrides(tenant_id: str, bookings: list[dict],
                           client=None) -> list[dict]:
    """Job parties (docs/modules/module-job-parties.md): where a job carries a trusted
    'pays' party, the draft bills THEM — stamped onto the booking BEFORE the
    pure builders group, so the grouping key, the tick values and the invoice
    identity all agree. One read for the whole batch; a tenant with no party
    rows pays nothing here."""
    from app import job_parties
    ids = [b["id"] for b in bookings if b.get("id")]
    by_booking = (db.job_parties_for_bookings(tenant_id, ids, client=client)
                  if ids else {})
    if not by_booking:
        return bookings
    for b in bookings:
        payer = job_parties.resolve(b, by_booking.get(b["id"], []))["pays"]
        if not payer.get("is_fallback") and payer.get("email"):
            b["contact"] = payer["email"]
            if payer.get("name"):
                b["contact_name"] = payer["name"]
    return bookings


def drafts_view(tenant_id: str, period_from: str, period_to: str, client=None) -> list[dict]:
    """The drafts 'Generate' would create for the period — delivered jobs only."""
    rate = db.get_rate_card(tenant_id, client=client) or dict(DEFAULT_RATE)
    bookings = db.list_completed_bookings(tenant_id, period_from, period_to, client=client)
    # Guard before the postcode lookup so already-billed jobs cost nothing to skip.
    bookings = _unbilled(tenant_id, bookings, client=client)
    bookings = _apply_party_overrides(tenant_id, bookings, client=client)
    codes = set()
    for b in bookings:
        for k in ("collection_postcode", "delivery_postcode"):
            if b.get(k):
                codes.add(b[k].upper())
    idx = {r["postcode"]: r for r in db.get_postcodes(sorted(codes)) if r.get("lat") is not None}
    _rate, _inc = _preview_tax(tenant_id, client)
    return drafts(bookings, rate, idx, vat_rate=_rate, include=_inc)


# ── Field Service (Trades) invoicing (docs/modules/module-field-service.md D4) ───────
# The team member sibling of the transport drafts above: completed jobs (engineer_id
# set) grouped by customer, each line at its stored PRE-VAT quote (quoted_amount,
# stamped when the job was booked), or recomputed from the trades rate card when a
# job was never quoted. The shared create_invoice adds VAT + mints the ref.

def _fsm_amount_for(booking: dict, rate_card: dict | None) -> float:
    """A job's PRE-VAT charge: its stored quote if it has one, else recomputed from
    the rate card by duration (so an unquoted-but-completed job still invoices)."""
    from app import fsm_pricing
    stored = booking.get("quoted_amount")
    if stored is not None:
        return round(float(stored), 2)
    return fsm_pricing.quote(rate_card, booking.get("duration_minutes") or 0)["net"]


def fsm_drafts(completed_jobs: list[dict], rate_card: dict | None,
               vat_rate: float = VAT_RATE, include: bool = False) -> list[dict]:
    """Group completed field-service jobs by customer into draft invoices, biggest
    first. Each draft = {customer_email, customer_name, lines[], subtotal, vat, total,
    count} — same shape as the transport drafts so the route + template are shared."""
    groups: dict = {}
    for b in completed_jobs:
        if not b.get("engineer_id"):
            continue
        key = (b.get("contact") or "").lower() or "—"
        g = groups.setdefault(key, {"customer_email": b.get("contact"),
                                    "customer_name": b.get("contact_name") or b.get("contact") or "Unknown customer",
                                    "lines": []})
        ref = b.get("booking_ref") or b["id"][:8]
        job = b.get("job_type") or "Job"
        g["lines"].append({
            "booking_id": b["id"],
            "description": f"{ref} · {job} — {(b.get('summary') or '')[:40]}",
            "amount": _fsm_amount_for(b, rate_card),
            "date": str(b.get("booking_date"))[:10],
        })
    return _group(groups, vat_rate, include)


def fsm_drafts_view(tenant_id: str, period_from: str, period_to: str, client=None) -> list[dict]:
    """The trades drafts 'Generate' would create for the period — completed jobs only."""
    from app.fsm_pricing import DEFAULT_FSM_RATE
    rate = db.get_fsm_rate_card(tenant_id, client=client) or dict(DEFAULT_FSM_RATE)
    jobs = db.list_completed_fsm_jobs(tenant_id, period_from, period_to, client=client)
    jobs = _unbilled(tenant_id, jobs, client=client)
    jobs = _apply_party_overrides(tenant_id, jobs, client=client)
    _rate, _inc = _preview_tax(tenant_id, client)
    return fsm_drafts(jobs, rate, vat_rate=_rate, include=_inc)


# ── Office / scheduling invoicing (appointments + stays) ─────────────────────
# The office sibling of the transport/trades builders: completed OFFICE bookings (no
# spaces, no team member) grouped by customer, each line at its stored PRE-VAT quote
# (quoted_amount — stamped when a priced service/stay was held), or 0 when the booking
# carried no price (the business edits the line before sending). Switched ON since the
# invoicing module was extracted from Field OS (2026-07-26) — invoicing is now a
# standalone core module for every tenant, not a Field OS bundle.
OFFICE_INVOICING_ENABLED = True


def draft_key(draft: dict) -> str:
    """Stable identifier for one draft in the generate form's customer selection.

    Email when there is one, else the display name — the builders group on whichever
    they have, and an office booking can carry a name with no address. Lower-cased so
    the posted value and the rebuilt draft compare the same way.
    """
    return ((draft.get("customer_email") or draft.get("customer_name")
             or "").strip().lower())


def select_drafts(drafts: list[dict], customers) -> list[dict]:
    """The drafts the user ticked. An EMPTY selection means all of them — that keeps
    the old "generate everything for the period" behaviour for any caller that posts
    no selection, and means a form rendered before this existed still works."""
    picked = {(c or "").strip().lower() for c in (customers or []) if (c or "").strip()}
    if not picked:
        return drafts
    return [d for d in drafts if draft_key(d) in picked]


def _unbilled(tenant_id: str, bookings: list[dict], client=None) -> list[dict]:
    """Drop completed work that is already on a live invoice — the duplicate-invoice
    guard, applied by every *_drafts_view before its builder runs.

    Without this, `list_completed_*` filters on status='completed' plus the date window
    and nothing else, so generating for July and then for June–August bills the same
    jobs twice. The key is the booking id (which all three builders already stamp onto
    their lines); a cancelled or voided invoice releases its bookings — see
    db.invoiced_booking_ids.

    Applied to the BOOKINGS rather than the finished drafts on purpose: the builders
    compute per-draft totals, so filtering afterwards would mean recomputing them, and
    the pure builders stay list-in/list-out for their unit tests.
    """
    if not bookings:
        return bookings
    billed = db.invoiced_booking_ids(tenant_id, [b["id"] for b in bookings], client=client)
    return [b for b in bookings if b["id"] not in billed] if billed else bookings


def _office_amount_for(booking: dict) -> float:
    """An office booking's PRE-VAT charge: its stored quote, else 0 (no office rate card to
    recompute from — an unpriced booking invoices at 0 for the business to edit)."""
    stored = booking.get("quoted_amount")
    return round(float(stored), 2) if stored is not None else 0.0


def office_drafts(completed_bookings: list[dict], vat_rate: float = VAT_RATE,
                  include: bool = False) -> list[dict]:
    """Group completed office bookings (appointments + stays) by customer into draft
    invoices, biggest first — same shape as the transport/trades drafts so the route +
    template are shared."""
    groups: dict = {}
    for b in completed_bookings:
        if b.get("spaces") is not None or b.get("engineer_id"):
            continue
        key = (b.get("contact") or "").lower() or "—"
        g = groups.setdefault(key, {"customer_email": b.get("contact"),
                                    "customer_name": b.get("contact_name") or b.get("contact") or "Unknown customer",
                                    "lines": []})
        ref = b.get("booking_ref") or b["id"][:8]
        what = (b.get("details") or {}).get("Service") or (b.get("summary") or "Booking")
        g["lines"].append({
            "booking_id": b["id"],
            "description": f"{ref} · {str(what)[:50]}",
            "amount": _office_amount_for(b),
            "date": str(b.get("booking_date"))[:10],
        })
    return _group(groups, vat_rate, include)


def office_drafts_view(tenant_id: str, period_from: str, period_to: str, client=None) -> list[dict]:
    """The office drafts 'Generate' would create for the period — completed appointments/stays."""
    bookings = db.list_completed_office_bookings(tenant_id, period_from, period_to, client=client)
    bookings = _unbilled(tenant_id, bookings, client=client)
    bookings = _apply_party_overrides(tenant_id, bookings, client=client)
    _rate, _inc = _preview_tax(tenant_id, client)
    return office_drafts(bookings, vat_rate=_rate, include=_inc)


# ── The line-builder registry (docs/modules/module-invoicing.md) ─────────────────────
# Invoicing is ONE shared surface; each billable vertical contributes only a draft
# builder (how its completed work is priced into lines). The shared route dispatches
# on the tenant's active module — a new vertical adds an entry here, not a new page.
# module → the *name* of its draft-builder (resolved late via globals() so the builder
# can be swapped/extended; a new vertical adds an entry + its *_drafts_view function).
_BUILDERS = {"tms": "drafts_view", "fsm": "fsm_drafts_view", "scheduling": "office_drafts_view"}


def billable_module(modules) -> str | None:
    """The vertical whose generate-from-completed-work draft builder applies (a tenant
    has one industry module), or None when none is active. Since the standalone
    extraction this gates only the GENERATE flow — the invoicing surface itself is
    core for every tenant (standalone/concierge-created invoices need no vertical)."""
    order = ["tms", "fsm"] + (["scheduling"] if OFFICE_INVOICING_ENABLED else [])
    return next((m for m in order if m in modules), None)


def office_invoicing_pending(modules) -> bool:
    """True when this tenant WOULD invoice via the office builder, but it isn't switched on
    yet — drives the hub's greyed 'coming soon' Invoicing card for appointment/stay tenants.
    Always False since the extraction (kept for the hub's dispatch shape)."""
    return (not OFFICE_INVOICING_ENABLED and "scheduling" in (modules or set())
            and billable_module(modules) is None)


def draft_view_for(tenant_id: str, modules, period_from: str, period_to: str,
                   client=None) -> list[dict]:
    """The draft invoices for the period, built by the tenant's active vertical."""
    mod = billable_module(modules)
    if not mod:
        return []
    return globals()[_BUILDERS[mod]](tenant_id, period_from, period_to, client=client)
