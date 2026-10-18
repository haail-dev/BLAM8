"""Storefront OS routes (docs/os/storefront.md §2, §7, §8) — the daily-rhythm surface.

Phase 1 scope: /app/shop (the daily surface — today's collections + stock/reorder),
the catalogue (list + add/edit + manual stock-in), and manual transactions (create,
reserve via the atomic RPC, mark ready/completed). The OCR intake (Phase 3), the
best-before "Draft specials" panel (Phase 4) and the drafter tools (Phase 2) are
DEFERRED — panel 2 shows a placeholder.

Every route gates on _require_storefront (db.module_ui_visible(tenant_id, "storefront") →
404 if off), the twin of dashboard._require_tms/_require_fsm/_require_practice.
Mutations carry CSRF. IDOR-safe by construction: every db.shop_* call filters on
ctx.tenant_id under the tenant's RLS client.
"""
import base64
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from app import agent, db, shop_ocr, usage
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.scheduling import DIARY_TZ
from app.security import require_csrf
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/shop", tags=["shop"])

# JSON API twin of src/app/crm_routes.py — /api/shop, Depends(require_onboarded) → TenantCtx,
# require_csrf on writes, ctx.tenant_id + client=ctx.client on every db.shop_* call so
# every route is IDOR-safe by construction (docs/os/storefront.md §7).
api = APIRouter(prefix="/api/shop", tags=["shop-api"])


def _order_facts(tx: dict) -> str:
    """The line items of a transaction as a short facts string for a receipt / decline draft."""
    items = ", ".join(
        f"{float(it.get('qty') or 1):g}× {it.get('product_name')}"
        for it in (tx.get("shop_transaction_items") or []) if it.get("product_name"))
    return items or "their order"


def _require_storefront(ctx: TenantCtx) -> None:
    """The gate for every Storefront surface. One subscriptions row (module='storefront')
    lights the whole shop — stock, collections and (later) best-before specials."""
    if not db.module_ui_visible(ctx.tenant_id, "storefront", client=ctx.client):
        raise HTTPException(status_code=404, detail="Storefront OS is not active for this account")


def _today_iso() -> str:
    return datetime.now(DIARY_TZ).date().isoformat()


def _num(v: str | None):
    """Parse a form number field → float or None (blank/invalid → None)."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── the daily surface (§2) ───────────────────────────────────────────────────
@router.get("")
def shop_home(request: Request, msg: str = "", err: str = "",
              ctx: TenantCtx = Depends(require_onboarded)):
    """The Storefront daily surface (§2): panel 1 today's collections, panel 2 the
    best-before specials placeholder (Phase 4), panel 3 stock levels + reorder flags.
    Derived server-side from shop_* — no new aggregation store."""
    _require_storefront(ctx)
    collections = db.shop_today_collections(ctx.tenant_id, _today_iso(), client=ctx.client)
    products = db.shop_list_products(ctx.tenant_id, client=ctx.client)
    low = [p for p in products if db._shop_is_low(p)]
    # Panel 2 (§12.2): expiring stock + how many past buyers each can nudge — the
    # count is the differentiator ("nudge 8 regulars who buy this"). A [Draft specials]
    # action per product posts to /api/shop/products/{id}/draft-specials.
    expiring = db.shop_products_expiring(ctx.tenant_id, client=ctx.client)
    for p in expiring:
        try:
            p["past_buyers"] = len(db.shop_past_buyers_of(ctx.tenant_id, p["id"], client=ctx.client))
        except Exception:
            p["past_buyers"] = 0
    return templates.TemplateResponse(request, "app/shop_home.html", _ctx(
        "shop", ctx, msg=msg, err=err, collections=collections,
        products=products, low=low, expiring=expiring))


# ── the catalogue (§7) ───────────────────────────────────────────────────────
@router.get("/products")
def products_page(request: Request, q: str = "", low: str = "", msg: str = "",
                  ctx: TenantCtx = Depends(require_onboarded)):
    """The full catalogue — search + low-stock filter, add/edit inline."""
    _require_storefront(ctx)
    products = db.shop_list_products(ctx.tenant_id, q=q.strip() or None,
                                     low_stock=(low == "1"), client=ctx.client)
    return templates.TemplateResponse(request, "app/shop_products.html", _ctx(
        "shop_products", ctx, products=products, q=q, low=(low == "1"), msg=msg))


@router.post("/products", dependencies=[Depends(require_csrf)])
def product_add(name: str = Form(""), unit: str = Form("each"),
                unit_price: str = Form(""), reorder_threshold: str = Form(""),
                supplier: str = Form(""), best_before_date: str = Form(""),
                ctx: TenantCtx = Depends(require_onboarded)):
    """Manual product add (dedupes on lower(name) — a repeat add tops up nothing,
    just returns the existing row)."""
    _require_storefront(ctx)
    name = (name or "").strip()
    if not name:
        return RedirectResponse("/app/shop/products?msg=err", status_code=303)
    prod = db.shop_get_or_create_product(
        ctx.tenant_id, name, unit=(unit or "each").strip() or "each",
        best_before_date=best_before_date.strip() or None,
        supplier=supplier.strip() or None, client=ctx.client)
    fields = {}
    if _num(unit_price) is not None:
        fields["unit_price"] = _num(unit_price)
    if _num(reorder_threshold) is not None:
        fields["reorder_threshold"] = _num(reorder_threshold)
    if fields:
        db.shop_update_product(prod["id"], ctx.tenant_id, fields, client=ctx.client)
    return RedirectResponse("/app/shop/products?msg=added", status_code=303)


@router.post("/products/{product_id}", dependencies=[Depends(require_csrf)])
def product_edit(product_id: str, unit: str = Form(""), unit_price: str = Form(""),
                 reorder_threshold: str = Form(""), supplier: str = Form(""),
                 best_before_date: str = Form(""), sell_before_lead_days: str = Form(""),
                 ctx: TenantCtx = Depends(require_onboarded)):
    """Edit price / threshold / best-before / supplier / lead. Never touches
    qty_on_hand — that moves only through the atomic stock-in / reserve RPCs."""
    _require_storefront(ctx)
    if not db.shop_get_product(product_id, ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Product not found")
    fields: dict = {}
    if unit.strip():
        fields["unit"] = unit.strip()
    if supplier.strip():
        fields["supplier"] = supplier.strip()
    if best_before_date.strip():
        fields["best_before_date"] = best_before_date.strip()
    if _num(unit_price) is not None:
        fields["unit_price"] = _num(unit_price)
    if _num(reorder_threshold) is not None:
        fields["reorder_threshold"] = _num(reorder_threshold)
    if _num(sell_before_lead_days) is not None:
        fields["sell_before_lead_days"] = int(_num(sell_before_lead_days))
    if fields:
        db.shop_update_product(product_id, ctx.tenant_id, fields, client=ctx.client)
    return RedirectResponse("/app/shop/products?msg=saved", status_code=303)


@router.post("/products/{product_id}/stock-in", dependencies=[Depends(require_csrf)])
def product_stock_in(product_id: str, qty: str = Form(""), best_before: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    """Manual stock-in for one product (the "photograph delivery" flow's manual twin,
    §2 panel 3). Routes through db.shop_apply_stock_in → the atomic increment RPC +
    a stock_movements row, so a manual top-up is audited exactly like an OCR one."""
    _require_storefront(ctx)
    prod = db.shop_get_product(product_id, ctx.tenant_id, client=ctx.client)
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")
    n = _num(qty)
    if not n or n <= 0:
        return RedirectResponse("/app/shop/products?msg=err", status_code=303)
    db.shop_apply_stock_in(
        ctx.tenant_id,
        [{"name": prod["name"], "qty": n, "unit": prod.get("unit") or "each",
          "best_before": best_before.strip() or None}],
        source="manual", source_ref=None)
    return RedirectResponse("/app/shop/products?msg=stocked", status_code=303)


# ── OCR stock-in: photo → stock, zero typing (§7, §12.1) ─────────────────────
@router.get("/stock-in")
def stock_in_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The camera/upload page. Empty on load; JS posts the photo to /api/shop/ocr and
    renders the confidence-banded review table (apply pre-checked, review amber, skip
    greyed) which then posts the owner's checked lines to /api/shop/ocr/{job}/apply.
    The whole "self-fills" moment — no typing until the owner corrects a mis-read."""
    _require_storefront(ctx)
    return templates.TemplateResponse(request, "app/shop_stock_in.html", _ctx("shop", ctx))


# ── transactions / orders (§7) ───────────────────────────────────────────────
@router.get("/transactions")
def transactions_page(request: Request, status: str = "", msg: str = "",
                      ctx: TenantCtx = Depends(require_onboarded)):
    """Order/reservation list, filterable by status."""
    _require_storefront(ctx)
    txns = db.shop_list_transactions(ctx.tenant_id, status=status.strip() or None,
                                     client=ctx.client)
    return templates.TemplateResponse(request, "app/shop_transactions.html", _ctx(
        "shop_transactions", ctx, txns=txns, status=status, msg=msg))


@router.post("/transactions", dependencies=[Depends(require_csrf)])
def transaction_create(product_id: str = Form(""), qty: str = Form("1"),
                       ready_date: str = Form(""), customer_email: str = Form(""),
                       reserve: str = Form(""),
                       ctx: TenantCtx = Depends(require_onboarded)):
    """Create a manual (walk-in/counter) transaction for a product. When `reserve=1`
    it goes through the ATOMIC reserve RPC (shop_hold_stock_if_available) — the
    out-of-stock None surfaces as a clear message; otherwise it's an enquiry (no stock
    held). The customer, when an email is given, is a CRM contact (§1)."""
    _require_storefront(ctx)
    prod = db.shop_get_product(product_id, ctx.tenant_id, client=ctx.client)
    if not prod:
        return RedirectResponse("/app/shop/transactions?msg=err", status_code=303)
    n = _num(qty) or 1
    contact_id = None
    email = (customer_email or "").strip().lower()
    if "@" in email:
        contact_id = db.crm_get_or_create_contact(ctx.tenant_id, email).get("id")

    if reserve == "1":
        tx = db.shop_hold_stock_if_available(
            ctx.tenant_id, product_id, n, contact_id=contact_id,
            ready_date=ready_date.strip() or None, channel="walk_in", source="manual")
        if tx is None:
            return RedirectResponse("/app/shop/transactions?msg=oos", status_code=303)
        return RedirectResponse("/app/shop/transactions?msg=reserved", status_code=303)

    db.shop_create_transaction(
        ctx.tenant_id, contact_id=contact_id, channel="walk_in", status="enquiry",
        ready_date=ready_date.strip() or None,
        items=[{"product_id": product_id, "product_name": prod["name"], "qty": n,
                "unit_price": prod.get("unit_price")}], client=ctx.client)
    return RedirectResponse("/app/shop/transactions?msg=created", status_code=303)


@router.post("/transactions/{tx_id}/status", dependencies=[Depends(require_csrf)])
async def transaction_status(tx_id: str, status: str = Form("completed"), next: str = Form(""),
                             ctx: TenantCtx = Depends(require_onboarded)):
    """Move a transaction along its lifecycle — the counter's [Mark ready] / [Complete]
    (and [Cancel]) buttons. 'completed' stamps completed_at; 'completed'/'cancelled' also
    fire the receipt / polite-decline draft (best-effort, Phase 2). A cancel goes through
    the atomic release helper so held stock returns to the shelf."""
    _require_storefront(ctx)
    status = status if status in ("ready", "completed", "declined", "cancelled") else "completed"
    tx = db.shop_get_transaction(tx_id, ctx.tenant_id, client=ctx.client)
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    if status in ("cancelled", "declined"):
        db.shop_release_reservation(tx_id, ctx.tenant_id, client=ctx.client)
        await _draft_touchpoint(ctx, tx, "cancel")
    else:
        db.shop_set_transaction_status(tx_id, ctx.tenant_id, status, client=ctx.client)
        if status == "completed":
            await _draft_touchpoint(ctx, tx, "complete")
    dest = next.strip() or "/app/shop/transactions"
    return RedirectResponse(f"{dest}?msg={status}", status_code=303)


# ── the drafter-adjacent JSON API (§7) ───────────────────────────────────────
_RECEIPT_PURPOSE = ("A warm, brief thank-you / receipt now the customer has collected their "
                    "order. Thank them for their custom and keep it short. Do NOT invent a "
                    "price, total or anything beyond the facts.")
_DECLINE_PURPOSE = ("A polite, brief note that the shop can't hold this order after all (the "
                    "items have gone back on the shelf). Apologise lightly and invite them to "
                    "get back in touch. Do NOT invent stock, prices or dates beyond the facts.")


def _contact_email(tx: dict, ctx: TenantCtx) -> str | None:
    """The reservation's customer email (for the receipt / decline draft), or None."""
    embedded = (tx.get("crm_contacts") or {}).get("email")
    if embedded:
        return embedded
    cid = tx.get("contact_id")
    if not cid:
        return None
    c = db.crm_get_contact(cid, ctx.tenant_id, client=ctx.client)
    return (c or {}).get("email")


async def _draft_touchpoint(ctx: TenantCtx, tx: dict, kind: str) -> str | None:
    """Draft the receipt ('complete') or polite decline ('cancel') for a transaction in the
    shop's voice via compose_outbound, queued to email_log(status='pending'). Best-effort:
    any failure (no contact, compose error) is swallowed so the status change still lands."""
    try:
        to = _contact_email(tx, ctx)
        if not to:
            return None
        cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
        facts = _order_facts(tx)
        if kind == "complete":
            purpose, subject, tag = _RECEIPT_PURPOSE, "Thanks for your order", "receipt"
            facts = f"The customer has collected: {facts}."
        else:
            purpose, subject, tag = _DECLINE_PURPOSE, "About your order", "decline"
            facts = f"The shop can no longer hold: {facts}."
        result = await agent.compose_outbound(cfg, purpose, facts,
                                              tenant_id=ctx.tenant_id, log_operation="reminder")
        log_id, _ = db.insert_email_log(
            ctx.tenant_id, to, subject, f"[{tag}] {facts}", result.text,
            status="pending", kind="reminder", thread_id=tx.get("thread_id"))
        db.log_agent_event(ctx.tenant_id, tag,
                           f"Drafted a {tag} for {to}", direction="outbound",
                           contact=to, subject=subject, email_log_id=log_id)
        usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                               operation="reminder", email_log_id=log_id)
        return log_id
    except Exception:
        log.warning("storefront %s draft failed for tx %s", kind, tx.get("id"), exc_info=True)
        return None


@api.post("/products/{product_id}/draft-specials", dependencies=[Depends(require_csrf)])
async def api_draft_specials(product_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Fire the best-before drafter for ONE product's batch right now (the daily-surface
    [Draft specials] button). Same compose/queue path as the 07:30 job: pull the DISTINCT
    past buyers of THIS product (deterministic SQL — do_not_sequence excluded), respect the
    per-contact/day cap (shop_had_special_today), and queue a personal 'special' draft
    (auto-sent for full-auto tenants). IDOR-safe: the product is fetched under ctx.tenant_id."""
    from app.jobs import send_best_before as bb
    _require_storefront(ctx)
    product = db.shop_get_product(product_id, ctx.tenant_id, client=ctx.client)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    auto = bool(cfg.get("auto_send"))
    discount = bb._discount(cfg)
    today = _today_iso()
    buyers = db.shop_past_buyers_of(ctx.tenant_id, product_id, client=ctx.client)
    drafted = sent = 0
    seen: set = set()
    for buyer in buyers:
        cid = buyer.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        try:
            if db.shop_had_special_today(ctx.tenant_id, cid, today, client=ctx.client):
                continue
            outcome = await bb._draft_one(ctx.tenant_id, cfg, auto, product, discount, buyer)
        except Exception:
            log.warning("draft-specials failed for contact %s (product %s)",
                        cid, product_id, exc_info=True)
            continue
        sent += outcome == "sent"
        drafted += outcome == "drafted"
    return {"ok": True, "product_id": product_id, "drafted": drafted, "sent": sent,
            "past_buyers": len(buyers)}


@api.post("/transactions/{tx_id}/reserve", dependencies=[Depends(require_csrf)])
def api_reserve(tx_id: str, product_id: str = Form(""), qty: str = Form("1"),
                ready_date: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    """Reserve stock against an EXISTING transaction via the atomic RPC (the same hold the
    drafter uses). Out-of-stock (RPC None) returns 409, never a fabricated hold."""
    _require_storefront(ctx)
    tx = db.shop_get_transaction(tx_id, ctx.tenant_id, client=ctx.client)
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    if not db.shop_get_product(product_id, ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Product not found")
    n = _num(qty) or 1
    row = db.shop_hold_stock_if_available(
        ctx.tenant_id, product_id, n, transaction_id=tx_id,
        contact_id=tx.get("contact_id"), ready_date=ready_date.strip() or None,
        channel=tx.get("channel") or "email", source="manual")
    if row is None:
        raise HTTPException(status_code=409, detail="out_of_stock")
    return {"ok": True, "transaction_id": tx_id, "status": "reserved"}


@api.post("/transactions/{tx_id}/complete", dependencies=[Depends(require_csrf)])
async def api_complete(tx_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Mark a transaction collected → stamps completed_at and drafts a receipt (best-effort)."""
    _require_storefront(ctx)
    tx = db.shop_get_transaction(tx_id, ctx.tenant_id, client=ctx.client)
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    db.shop_set_transaction_status(tx_id, ctx.tenant_id, "completed", client=ctx.client)
    log_id = await _draft_touchpoint(ctx, tx, "complete")
    return {"ok": True, "transaction_id": tx_id, "status": "completed", "draft_id": log_id}


@api.post("/transactions/{tx_id}/cancel", dependencies=[Depends(require_csrf)])
async def api_cancel(tx_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Release the reservation's held stock (atomic release helper) and draft a polite
    decline (best-effort)."""
    _require_storefront(ctx)
    tx = db.shop_get_transaction(tx_id, ctx.tenant_id, client=ctx.client)
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    db.shop_release_reservation(tx_id, ctx.tenant_id, client=ctx.client)
    log_id = await _draft_touchpoint(ctx, tx, "cancel")
    return {"ok": True, "transaction_id": tx_id, "status": "cancelled", "draft_id": log_id}


# ── OCR intake API (the wedge — §7, §12.1) ────────────────────────────────────
# STORAGE SEAM: the codebase has NO Supabase Storage bucket wired (POD/driver
# photo_url is a plain text column; grep finds no .storage.from_ anywhere), so per
# the build note we DON'T stand up a bucket — the extraction runs INLINE on the
# uploaded bytes, and the durable, auditable record is the extracted lines (with
# their confidence) on shop_ocr_jobs.extracted + the stock_movements ledger. v1
# never needs to re-read the image, so image_path stays a nullable reference the
# email-attachment path (deferred) or a future bucket can populate.
def _ocr_lines_from_form(form) -> list[dict]:
    """Reconstruct the owner's reviewed lines from the apply form: only rows they left
    CHECKED become stock. Each checked row carries name/qty/unit/best_before/unit_price
    hidden fields (index i). A row is applied iff line_checked_{i} is present."""
    lines, i = [], 0
    while f"line_name_{i}" in form:
        if form.get(f"line_checked_{i}"):
            qty = _num(form.get(f"line_qty_{i}"))
            name = (form.get(f"line_name_{i}") or "").strip()
            if name and qty:
                lines.append({
                    "name": name, "qty": qty,
                    "unit": (form.get(f"line_unit_{i}") or "each").strip() or "each",
                    "best_before": (form.get(f"line_best_before_{i}") or "").strip() or None,
                    "unit_price": _num(form.get(f"line_unit_price_{i}")),
                })
        i += 1
    return lines


@api.post("/ocr", dependencies=[Depends(require_csrf)])
async def api_ocr(request: Request, image: UploadFile = File(...),
                  kind: str = Form("delivery_note"),
                  ctx: TenantCtx = Depends(require_onboarded)):
    """Upload a photo → create a shop_ocr_jobs row → run the vision extraction INLINE →
    store the banded lines → return them for the owner's review. Nothing writes to
    stock here (the apply tap does). IDOR-safe: the job is created + read under
    ctx.tenant_id."""
    _require_storefront(ctx)
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="No image received")
    image_b64 = base64.b64encode(data).decode()
    job = db.shop_create_ocr_job(ctx.tenant_id, kind, image_path=None, client=ctx.client)
    result = await shop_ocr.extract_lines(image_b64, kind, tenant_id=ctx.tenant_id)
    db.shop_set_ocr_extracted(job["id"], ctx.tenant_id, result["lines"], client=ctx.client)
    return {"ok": True, "job_id": job["id"], "lines": result["lines"],
            "document_note": result.get("document_note", "")}


@api.post("/ocr/{job_id}/apply", dependencies=[Depends(require_csrf)])
async def api_ocr_apply(job_id: str, request: Request,
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Commit the owner's reviewed + CHECKED lines to stock: shop_apply_stock_in(
    source='ocr', source_ref=job_id) get-or-creates each product, bumps qty_on_hand via
    the atomic RPC, and writes a stock_movements row — then the job is marked applied.
    DEDUPE: applying an already-applied job is a no-op (the trust bridge + idempotency)."""
    _require_storefront(ctx)
    job = db.shop_get_ocr_job(job_id, ctx.tenant_id, client=ctx.client)
    if not job:
        raise HTTPException(status_code=404, detail="OCR job not found")
    if job.get("status") == "applied":
        return {"ok": True, "job_id": job_id, "status": "applied",
                "applied": {"created": 0, "updated": 0, "movements": 0}, "deduped": True}
    form = await request.form()
    lines = _ocr_lines_from_form(form)
    applied = db.shop_apply_stock_in(ctx.tenant_id, lines, source="ocr", source_ref=job_id) \
        if lines else {"created": 0, "updated": 0, "movements": 0}
    db.shop_mark_ocr_applied(job_id, ctx.tenant_id, client=ctx.client)
    return {"ok": True, "job_id": job_id, "status": "applied", "applied": applied}
