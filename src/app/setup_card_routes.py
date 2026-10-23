"""Authenticated card editing, preview and atomic settings acceptance."""
from hashlib import sha256
import asyncio
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import db, setup_cards as cards, setup_card_store as store
from app import settings_recommend, setup_rows, intake, setup_card_scalars, setup_card_invoices, setup_card_crm, setup_card_mode
from app.auth import require_onboarded, TenantCtx
from app.security import rate_limit, require_csrf_header

log = logging.getLogger(__name__)
from app.device_sync import SyncRoute
router = APIRouter(prefix="/setup-cards", route_class=SyncRoute, dependencies=[
    Depends(rate_limit("setup_cards", limit=60, window_s=60))])


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    changes: list[dict] = Field(min_length=1, max_length=cards.MAX_LINES)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    ids: list[str] = Field(min_length=1, max_length=cards.MAX_LINES)


class AcceptRequest(Selection):
    settings_version: str = Field(min_length=64, max_length=64)


def _version(cfg):
    return sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


REFUSED = "BLAM8_SETUP_INVALID"
BEHIND = ("The saved settings could not take this change. The server's setup functions may be behind "
          "the application; ask support to check the database migrations.")


def _refusal(exc):
    """The owner-facing sentence for a PostgREST error raised as BLAM8_SETUP_INVALID, else None.

    A bare code means the function rejected the shape of the change, which is what an
    older installed function does with a newer patch. A reason after the colon is the
    function's own explanation of a particular refusal, so it is passed on as written.
    """
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not message.startswith(REFUSED):
        return None
    reason = message[len(REFUSED):].lstrip(": ").strip()
    return f"The saved settings could not take this change: {reason.rstrip('.')}." if reason else BEHIND


def _error(exc):
    if isinstance(exc, cards.Stale):
        raise HTTPException(409, str(exc)) from None
    if isinstance(exc, cards.Invalid):
        raise HTTPException(422, str(exc)) from None
    if isinstance(exc, HTTPException):
        raise exc
    refusal = _refusal(exc)
    if refusal:
        # The database refused a change the application built. Repeating it, or
        # reloading the card, cannot help, so say so instead of "could not be confirmed".
        log.warning("Setup card change refused by the database: %s", refusal, exc_info=True)
        raise HTTPException(422, refusal) from None
    log.warning("Setup card operation could not be verified", exc_info=True)
    raise HTTPException(503, "The operation could not be confirmed. Reload the card before trying again.") from None


def _selected(page, body, ctx):
    if page in {"crm_settings", "crm_fields"} and "crm" not in db.visible_modules(ctx.tenant_id, client=ctx.client):
        raise HTTPException(404, "CRM is not available for this account.")
    if page == "diary_settings" and "scheduling" not in db.visible_modules(ctx.tenant_id, client=ctx.client):
        raise HTTPException(404, "Scheduling is not available for this account.")
    current = store.load(ctx.tenant_id, page, client=ctx.client)
    if not current or current["revision"] != body.revision:
        raise cards.Stale("The card changed. Reload before accepting it.")
    if page == 'dependants' and any(r['id'] in body.ids and r['name'] == 'ladder' and r['fields'].get('who') == 'contact' for r in current['lines']):
        if 'crm' not in db.visible_modules(ctx.tenant_id, client=ctx.client):
            raise HTTPException(404, 'CRM is not available for this account.')
    cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
    return current, _snapshot(page, cfg, ctx, card=current)


def _snapshot(page, cfg, ctx, groups=None, card=None):
    if page == 'knowledge':
        from app import setup_card_knowledge
        return setup_card_knowledge.snapshot(ctx.tenant_id, card, ctx.client)
    if page == "crm_fields":
        definitions = db.crm_list_field_definitions(ctx.tenant_id, entity="contact", client=ctx.client)
        return {"definitions": sorted(definitions, key=lambda r: r["id"])}
    if page == "crm_settings":
        config = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        stages = db.crm_list_stages(ctx.tenant_id, client=ctx.client)
        return {"quiet_days": config.get("crm_quiet_days"), "stages": sorted(stages, key=lambda r: r["id"])}
    if page in {"invoice_settings", "packages"}:
        row = db.get_invoice_settings(ctx.tenant_id, client=ctx.client) or {}
        from app import setup_card_packages
        if cfg and page == "packages" and setup_card_packages.pending(card):
            return {**row, "_setup_scheduling": cfg}
        return row
    if page == "diary_settings":
        snapshot = dict(cfg or {})
        if setup_card_scalars.needs_stages(card):
            stages = db.crm_list_stages(ctx.tenant_id, client=ctx.client)
            snapshot["_setup_stages"] = sorted(stages, key=lambda r: r["id"])
        if setup_card_mode.pending(card):
            snapshot["_setup_has_bookings"] = db.setup_has_booking_history(ctx.tenant_id, client=ctx.client)
        return snapshot
    if page == "booking_details":
        definitions = db.crm_list_field_definitions(ctx.tenant_id, entity="contact", client=ctx.client)
        snapshot = {"config": cfg or {}, "definitions": sorted(definitions, key=lambda r: r["id"])}
        if setup_card_mode.pending(card):
            snapshot['has_bookings'] = db.setup_has_booking_history(ctx.tenant_id, client=ctx.client)
        return snapshot
    if groups is None:
        groups = db.list_requirement_groups(ctx.tenant_id, client=ctx.client, strict=True)
    snapshot = {"config": cfg or {}, "groups": sorted(groups, key=lambda r: r["id"])}
    from app import setup_card_ladders
    if setup_card_ladders.pending(card):
        snapshot['definitions'] = sorted(db.crm_list_field_definitions(ctx.tenant_id, entity='contact', client=ctx.client), key=lambda r: r['id'])
    return snapshot


def _preview(card, ids, cfg):
    _, changes = cards.acceptance_patch(card, ids, cfg)
    for change in changes:
        if "fields" not in change:
            change["fields"] = setup_rows.parse(card["page"], change["name"], cards._named(change["name"], change["after"]))["fields"]
        change["display"] = setup_rows.describe(card["page"], change["name"], change["fields"])
    return {"settings_version": _version(cfg), "changes": changes}


@router.get("")
def read_cards(ctx: TenantCtx = Depends(require_onboarded)):
    """One verified read supplies effective values and bounded batch previews."""
    try:
        current = store.load_all(ctx.tenant_id, client=ctx.client)
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
        groups = db.list_requirement_groups(ctx.tenant_id, client=ctx.client, strict=True)
        modules = db.visible_modules(ctx.tenant_id, client=ctx.client)
        allowed = "scheduling" in modules
        views = {}
        from app import setup_catalogue
        scalar_specs = {k: v for specs in setup_catalogue.scalar_specs().values() for k, v in specs.items()}
        for page, card in current.items():
            if page not in cards.PAGES:
                continue
            spec = settings_recommend.PAGES[page]
            view = {"label": spec["label"], "href": spec["href"], "rows": {}, "all": None,
                    "stay": intake.stay_mode(cfg)}
            pending = [v for v in card["lines"] if v["status"] in cards.PENDING]
            snapshot = _snapshot(page, cfg, ctx, groups, card) if pending else None
            if page == "diary_settings" and snapshot and "_setup_stages" in snapshot:
                for key in setup_card_scalars.STAGE_KEYS:
                    scalar_specs[key] = {**scalar_specs[key], "type": "choice",
                                         "choices": [s["name"] for s in snapshot["_setup_stages"]]}
            for ids in [[v["id"] for v in pending]] + [[v["id"]] for v in pending]:
                if not ids:
                    continue
                try:
                    if page == "diary_settings" and not allowed:
                        raise cards.Invalid("Immediate acceptance needs the Scheduling module.")
                    if page in {"crm_settings", "crm_fields"} and "crm" not in modules:
                        raise cards.Invalid("Immediate acceptance needs the CRM module.")
                    if page == 'dependants' and 'crm' not in modules and any(r['id'] in ids and r['name'] == 'ladder' and r['fields'].get('who') == 'contact' for r in pending):
                        raise cards.Invalid('Customer progress needs the CRM module.')
                    preview = _preview(card, ids, snapshot)
                except cards.Invalid as exc:
                    preview = {"error": str(exc)}
                if len(ids) == len(pending) and ids == [v["id"] for v in pending]:
                    view["all"] = preview
                if len(ids) == 1:
                    view["rows"][ids[0]] = preview
            views[page] = view
        return {"cards": [c for p, c in current.items() if p in cards.PAGES], "views": views,
                "options": {"services": ([intake.STAYS_SCOPE] if intake.stay_mode(cfg) else []) +
                                         [r["name"] for r in (cfg or {}).get("service_types") or []],
                            "resources": [r["name"] for r in (cfg or {}).get("resource_types") or []],
                            "groups": [g["label"] for g in groups], "settings": scalar_specs,
                            "controls": setup_catalogue.controls()}}
    except Exception as exc:
        _error(exc)


@router.get("/{page}")
def read_card(page: str, ctx: TenantCtx = Depends(require_onboarded)):
    try:
        return {"card": store.load(ctx.tenant_id, page, client=ctx.client)}
    except Exception as exc:
        _error(exc)


@router.post("/{page}/changes", dependencies=[Depends(require_csrf_header)])
def change_card(page: str, body: ChangeRequest, ctx: TenantCtx = Depends(require_onboarded)):
    try:
        current = store.load(ctx.tenant_id, page, client=ctx.client)
        card = cards.change(page, current, body.revision, str(uuid4()), body.changes)
        from app.guided_setup_routes import commit_linked_card
        linked = commit_linked_card(ctx, page, body.revision, card)
        return {"card": linked or store.save(ctx.tenant_id, page, body.revision, card, client=ctx.client)}
    except Exception as exc:
        _error(exc)


@router.get("/{page}/history")
def read_history(page: str, before: int | None = Query(default=None, ge=1),
                 ctx: TenantCtx = Depends(require_onboarded)):
    try:
        revisions = store.history(ctx.tenant_id, page, before=before, client=ctx.client)
        return {"revisions": revisions,
                "before": revisions[-1]["revision"] if len(revisions) == 50 else None}
    except Exception as exc:
        _error(exc)


@router.post("/{page}/preview", dependencies=[Depends(require_csrf_header)])
def preview_card(page: str, body: Selection, ctx: TenantCtx = Depends(require_onboarded)):
    try:
        current, cfg = _selected(page, body, ctx)
        return {"revision": current["revision"], **_preview(current, body.ids, cfg)}
    except Exception as exc:
        _error(exc)


@router.post("/{page}/accept", dependencies=[Depends(require_csrf_header)])
def accept_card(page: str, body: AcceptRequest, ctx: TenantCtx = Depends(require_onboarded)):
    try:
        if page == "crm_fields":
            from app.dashboard import require_crm_write
            require_crm_write(ctx)
        current, cfg = _selected(page, body, ctx)
        if _version(cfg) != body.settings_version:
            raise cards.Stale("Saved settings changed since the preview. Review the new changes first.")
        card, patch = cards.accept(current, body.revision, str(uuid4()), body.ids, cfg, actor=ctx.user_id)
        if page == 'dependants' and patch.get('definitions'):
            if 'crm' not in db.visible_modules(ctx.tenant_id, client=ctx.client):
                raise HTTPException(404, 'CRM is not available for this account.')
            from app.dashboard import require_crm_write
            require_crm_write(ctx)
        if page == 'knowledge':
            from app import setup_card_knowledge, usage
            from app.config import settings
            patch, tokens = asyncio.run(setup_card_knowledge.prepare(patch))
            usage.record_voyage(ctx.tenant_id, settings.voyage_model, tokens, operation='embed_ingest')
        from app.guided_setup_routes import commit_linked_card
        linked = commit_linked_card(ctx, page, body.revision, card, snapshot=cfg, patch=patch)
        return {"card": linked or store.save(ctx.tenant_id, page, body.revision, card,
                                  expected_settings=cfg if cfg is not None else {}, settings_patch=patch, client=ctx.client)}
    except Exception as exc:
        _error(exc)
