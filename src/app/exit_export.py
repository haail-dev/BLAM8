"""The exit door — leave with everything, in one download.

The competitive research (2026-09-01) found two trust vacuums no vendor fills:
nobody is praised for price stability, and nobody is praised for a dignified
exit — the sector's horror stories are deleted histories on cancellation and
CSVs held hostage. This module is the product half of the answer (the landing
page's price promise is the other): one zip holding the tenant's contacts,
money documents, diary and knowledge.

⚠ The router deliberately does NOT carry require_subscription: an export that
stops working the day you stop paying is a turnstile, not an exit door. Auth
still applies (require_onboarded), and every read goes through the caller's
JWT-scoped client so RLS stays the second wall.

Sections are gathered best-effort and the MANIFEST says exactly what made it
in — a bundle that silently omits a section would break the very promise it
exists to keep.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response

from app import db, invoice_config, invoice_export
from app.auth import TenantCtx, require_onboarded
from app import guards as _guards

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["exit"])


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")   # Excel-friendly, like invoice_export
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def quotes_csv(quotes: list[dict]) -> str:
    """Every quote, lossless-enough to re-key nowhere: refs, states, money,
    the acceptance record and the customer's own PO."""
    return _csv(
        ["quote_ref", "status", "issue_date", "valid_until", "customer_name",
         "customer_email", "customer_reference", "title", "subtotal", "vat",
         "total", "deposit_percent", "accepted_name", "accepted_at",
         "declined_at", "view_count", "invoice_id"],
        [[q.get("quote_ref") or "", q.get("status") or "",
          (q.get("issue_date") or "")[:10], (q.get("valid_until") or "")[:10],
          q.get("customer_name") or "", q.get("customer_email") or "",
          q.get("customer_reference") or "", q.get("title") or "",
          f"{float(q.get('subtotal') or 0):.2f}", f"{float(q.get('vat') or 0):.2f}",
          f"{float(q.get('total') or 0):.2f}",
          q.get("deposit_percent") or "", q.get("accepted_name") or "",
          (q.get("accepted_at") or "")[:10], (q.get("declined_at") or "")[:10],
          q.get("view_count") or 0, q.get("invoice_id") or ""]
         for q in quotes])


def contacts_csv(contacts: list[dict]) -> str:
    """Same shape as the CRM page's own export, uncapped."""
    return _csv(
        ["name", "email", "company", "first_seen", "last_activity", "custom_data"],
        [[c.get("name") or "", c.get("email") or "",
          (c.get("crm_companies") or {}).get("name") or "",
          (c.get("first_seen_at") or "")[:10],
          (c.get("last_activity_at") or "")[:10],
          json.dumps(c.get("custom_data") or {})]
         for c in contacts])


def knowledge_csv(chunks: list[dict]) -> str:
    return _csv(["source", "created_at", "content"],
                [[c.get("source") or "", (c.get("created_at") or "")[:10],
                  c.get("content") or ""] for c in chunks])


README = """Your BLAM8 export
=================

Everything BLAM8 holds for your business, in one place:

  invoices.csv   Every invoice, line by line, with statuses, payment dates and
                 your customers' PO references — the same lossless "accountant"
                 format the in-app export produces.
  quotes.csv     Every quote: amounts, validity, who accepted and when.
  contacts.csv   Your customers — names, emails, companies, tracked fields.
  crm.json       The full CRM record: companies, deals, stages, activity
                 history, promises, custom fields.
  bookings.json  Your diary, raw and complete.
  knowledge.csv  Your knowledge base — the prices, policies and FAQs.

This download works whether or not your subscription is active. Your data is
yours; taking it with you was never meant to be the hard part.
"""


def build_bundle(sections: dict[str, str | bytes], skipped: list[str]) -> bytes:
    """Assemble the zip. `sections` maps filename → content; `skipped` names
    sections that could not be read — declared in the MANIFEST, never silent."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    manifest = [f"BLAM8 export — generated {stamp}", ""]
    manifest += [f"  included: {name}" for name in sections]
    manifest += [f"  UNAVAILABLE: {name} (could not be read — contact support "
                 "and we'll get it to you)" for name in skipped]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        z.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")
        for name, content in sections.items():
            z.writestr(name, content)
    return buf.getvalue()


@router.get("/export.zip")
def exit_bundle(ctx: TenantCtx = Depends(require_onboarded)):
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    sections: dict[str, str | bytes] = {}
    skipped: list[str] = []

    def gather(name: str, fn):
        try:
            sections[name] = fn()
        except Exception:
            log.warning("exit export: %s failed for %s", name, ctx.tenant_id,
                        exc_info=True)
            skipped.append(name)

    def _invoices():
        invoices = db.list_invoices(ctx.tenant_id, client=ctx.client)
        lines = db.invoice_lines_by_invoice(
            ctx.tenant_id, [i["id"] for i in invoices], client=ctx.client)
        s = invoice_config.settings_for(
            db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
        return invoice_export.build_csv("accountant", invoices, lines, s)

    gather("invoices.csv", _invoices)
    gather("quotes.csv", lambda: quotes_csv(
        db.list_quotes(ctx.tenant_id, client=ctx.client)))
    gather("contacts.csv", lambda: contacts_csv(
        db.crm_list_contacts(ctx.tenant_id, limit=100000, client=ctx.client)))
    gather("crm.json", lambda: json.dumps(
        db.crm_export(ctx.tenant_id, client=ctx.client), indent=1, default=str))
    gather("bookings.json", lambda: json.dumps(
        db.list_bookings(ctx.tenant_id, "2000-01-01", client=ctx.client),
        indent=1, default=str))
    gather("knowledge.csv", lambda: knowledge_csv(
        db.export_knowledge_chunks(ctx.tenant_id, client=ctx.client)))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        build_bundle(sections, skipped), media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="blam8-export-{stamp}.zip"'})
