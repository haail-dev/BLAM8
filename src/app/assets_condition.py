"""Assets OS — the condition photo-diff pipeline (docs/os/assets.md §6.3/§12b).

THE killer feature: a before/after LLM photo comparison that produces damage-claim
evidence. Triggered on insert_condition_report(phase='in') at return — we fetch the
handover baseline (db.latest_out_report), pair the return photos with it BY ORDINAL
position (out[i] ↔ in[i] — the capture UI prompts the same fixed shot list per unit
category, so ordering is meaningful), and run ONE multimodal Anthropic call per pair.

The vision call mirrors src/app/shop_ocr.py (the Storefront OCR pattern) but with TWO image
blocks — a BEFORE and an AFTER — and the §12b SYSTEM prompt VERBATIM. Structured
json_schema output, Sonnet (settings.anthropic_booking_model), metered via
usage.record_anthropic(operation='condition_diff') — the same books as crm_extract.

Confidence gate (§12b, thresholds 0.80 / 0.55 — the crm_fields.gate three-band shape):
  * ≥0.80 AND severity ≥ moderate → open a damage_case (status='open') + draft the
    "we found X, evidence, proposing £Y deduction" outbound. FORCED PENDING — a
    deduction claim is NEVER auto-sent regardless of cfg.auto_send (deposits are
    dispute-prone; the owner approves).
  * 0.55–0.80 → open the case flagged "review the photos", NO draft.
  * <0.55 or empty findings → clean return → draft the deposit-REFUND note.

The model is an EVIDENCE ASSISTANT, never the adjudicator (§14): every deduction is
human-approved; no baseline (no out-report) → no diff, no claim. GRACEFUL: any
vision/storage failure → the case is created "needs manual review", never crashes.
"""
from __future__ import annotations

import base64
import json
import logging

from app import db, llm, storage, usage
from app.agent import client
from app.config import settings

log = logging.getLogger(__name__)

# The §12b confidence gate thresholds (the crm_fields.gate three-band shape, but with
# Assets' own cut-offs — deposits warrant a higher bar than CRM autofill: 0.80 / 0.55).
DIFF_APPLY_AT = 0.80    # ≥ this + severity ≥ moderate → open a case + draft a claim
DIFF_REVIEW_AT = 0.55   # ≥ this (< APPLY) → open a review-only case, no draft

SEVERITY_ORDER = {"none": 0, "minor": 1, "moderate": 2, "major": 3}

# Phase D (§11/§12b): per-category repair-cost defaults → a SUGGESTED proposed_deduction.
# The tenant sets a map cfg['assets_repair_costs'] = {"<category>": {"minor": £, "moderate":
# £, "major": £}} (editable, per-category). When a damage_case opens we SUM (severity → £)
# over the findings to seed proposed_deduction. This is a SUGGESTION ONLY — still human-
# approved, still never auto-sent (Phase C's guarantees hold). No config → None (today's
# behaviour: the owner types the figure). A "_default" category key is the fallback.
DEFAULT_REPAIR_COST_KEY = "_default"


def repair_cost_for(cfg: dict | None, category: str | None, findings: list[dict]) -> float | None:
    """The SUGGESTED deduction (§12b): sum over findings of the category's per-severity
    repair-cost default. Pure. Returns None when the tenant has set NO repair-cost config
    (leave proposed_deduction=None — today's behaviour), so a suggestion only ever appears
    when the owner has opted in by configuring costs. A finding whose severity has no cost
    contributes 0; the category falls back to a '_default' map when its own isn't set."""
    costs = (cfg or {}).get("assets_repair_costs")
    if not isinstance(costs, dict) or not costs:
        return None                              # no config → no suggestion (owner types it)
    table = costs.get((category or "").lower()) or costs.get(DEFAULT_REPAIR_COST_KEY)
    if not isinstance(table, dict) or not table:
        return None
    total = 0.0
    matched = False
    for f in findings or []:
        sev = (f.get("severity") or "").lower()
        raw = table.get(sev)
        if raw is None:
            continue
        try:
            total += float(raw)
            matched = True
        except (TypeError, ValueError):
            continue
    return round(total, 2) if matched else None

# Structured-output schema for the vision call — one findings array (the crm_extract /
# shop_ocr output_config json_schema seam), each finding the §12b shape.
DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "change": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["minor", "moderate", "major"]},
                    "confidence": {"type": "number"},
                },
                "required": ["area", "change", "severity", "confidence"],
                "additionalProperties": False,
            },
        },
        "clean_return_note": {"type": "string"},
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# The §12b SYSTEM prompt VERBATIM — the "report ONLY new damage in AFTER, ignore
# lighting/angle, never invent" rules are load-bearing (the trust bridge).
_PROMPT = (
    "You compare a BEFORE and an AFTER photo of the same hired item from the same "
    "viewpoint. Report ONLY new damage or material change visible in AFTER that is NOT "
    "in BEFORE — scratches, dents, stains, breakage, missing parts, wear beyond fair "
    "use. Ignore lighting, angle and reflection differences. Never invent damage. For "
    "each finding return {area, change, severity(minor|moderate|major), confidence(0-1)}. "
    "If AFTER matches BEFORE, return an empty findings list with a clean-return note."
)


def _gate(confidence) -> str:
    """'apply' | 'review' | 'skip' for a diff confidence — the crm_fields.gate three-band
    shape (autofill/suggest/skip) at Assets' own §12b thresholds (0.80 / 0.55). Deposits
    warrant a higher bar than CRM autofill (0.75 / 0.5), so the cut-offs move but the
    banding shape is the same one the whole platform's trust gate runs on."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "skip"
    if c >= DIFF_APPLY_AT:
        return "apply"
    if c >= DIFF_REVIEW_AT:
        return "review"
    return "skip"


def _max_severity(findings: list[dict]) -> str:
    """The worst severity across findings — the case's headline severity (§12b step 3)."""
    worst = "none"
    for f in findings or []:
        s = (f.get("severity") or "none").lower()
        if SEVERITY_ORDER.get(s, 0) > SEVERITY_ORDER.get(worst, 0):
            worst = s
    return worst


async def _diff_pair(out_path: str, in_path: str, category: str | None,
                     *, tenant_id: str | None) -> list[dict]:
    """ONE multimodal vision call on a single BEFORE/AFTER pair → its findings (each
    carrying the pair's out/in paths). Mirrors shop_ocr.extract_lines but with TWO image
    blocks. GRACEFUL: a storage-download or model failure raises so the caller can mark
    the whole case "needs manual review" (it never fabricates a clean pass)."""
    before = base64.b64encode(storage.download_bytes(out_path)).decode("ascii")
    after = base64.b64encode(storage.download_bytes(in_path)).decode("ascii")
    user_text = ("Compare these two photos of the same hired item"
                 + (f" (category: {category})" if category else "")
                 + ". The first is BEFORE (handover), the second is AFTER (return). "
                   "Report only new damage in AFTER, allowing for fair wear and tear.")
    msg = await llm.create(
        client=client(), tenant_id=tenant_id, operation="condition_diff", module="assets",
        model=settings.anthropic_booking_model,
        max_tokens=1500,
        system=_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": before}},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": after}},
            {"type": "text", "text": user_text},
        ]}],
        output_config={"format": {"type": "json_schema", "schema": DIFF_SCHEMA}},
    )
    if tenant_id:
        try:
            usage.record_anthropic(tenant_id, msg.model, msg.usage,
                                   operation="condition_diff", module="assets")
        except Exception:  # metering must never break the diff
            log.warning("condition_diff usage metering failed for tenant %s",
                        tenant_id, exc_info=True)
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("condition_diff returned non-JSON for tenant %s", tenant_id)
        return []
    out = []
    for f in (data.get("findings") or []):
        try:
            conf = max(0.0, min(1.0, float(f.get("confidence"))))
        except (TypeError, ValueError):
            conf = 0.0
        sev = (f.get("severity") or "minor").lower()
        if sev not in SEVERITY_ORDER:
            sev = "minor"
        area = str(f.get("area") or "").strip()
        change = str(f.get("change") or "").strip()
        if not change:
            continue  # a finding with no described change is unusable — never a claim
        out.append({"area": area or "item", "change": change, "severity": sev,
                    "confidence": conf, "out_url": out_path, "in_url": in_path})
    return out


async def run_diff(tenant_id: str, in_report: dict, *, unit: dict | None = None,
                   cfg: dict | None = None) -> dict:
    """Compare a return ('in') report against its handover ('out') baseline and decide
    the outcome. Pure of routing — returns a verdict the caller (route/sweep) acts on:

        {"outcome": "damage_claim"|"review"|"clean"|"no_baseline"|"manual_review",
         "damage_case": <inserted row or None>, "findings": [...],
         "diff_confidence": float, "severity": str, "proposed_deduction": float|None}

    * no_baseline → no out-report captured → NO diff, NO claim possible (§14). The caller
      leaves the deposit alone (the deduct action is hard-blocked elsewhere).
    * manual_review → a storage/vision failure — a case is still opened flagged for a
      human so the return isn't silently passed as clean.
    * damage_claim (≥0.80 + severity≥moderate), review (0.55–0.80), clean (<0.55/empty).

    When the tenant has set per-category repair costs (cfg['assets_repair_costs'], §12b),
    a case opens with a SUGGESTED proposed_deduction summed from those defaults — a hint
    only, still human-approved, still never auto-sent. No config → proposed_deduction=None
    (today's behaviour, the owner types it). cfg=None also → None.
    """
    booking_id = in_report.get("booking_id")
    unit_id = in_report.get("unit_id")
    category = (unit or {}).get("category")
    out_report = db.latest_out_report(tenant_id, booking_id) if booking_id else None
    if not out_report:
        # No handover baseline → no diff, no claim. The hire shows "no baseline captured".
        return {"outcome": "no_baseline", "damage_case": None, "findings": [],
                "diff_confidence": None, "severity": "none", "proposed_deduction": None}

    out_paths = list(out_report.get("photo_urls") or [])
    in_paths = list(in_report.get("photo_urls") or [])
    pairs = list(zip(out_paths, in_paths))  # pair by ORDINAL position (§12b step 1)

    findings: list[dict] = []
    failed = False
    if not pairs:
        # Baseline exists but one side has no usable photos (e.g. storage_error at
        # capture) — can't diff. Flag for manual review rather than pass as clean.
        failed = True
    for out_path, in_path in pairs:
        try:
            findings.extend(await _diff_pair(out_path, in_path, category, tenant_id=tenant_id))
        except Exception:
            log.warning("condition_diff pair failed (tenant %s, booking %s)",
                        tenant_id, booking_id, exc_info=True)
            failed = True

    severity = _max_severity(findings)
    # Overall diff confidence = the max finding confidence (the strongest evidence drives
    # the gate); no findings → 0.0 (clean, unless a failure forced manual review).
    diff_confidence = max((f["confidence"] for f in findings), default=0.0)
    band = _gate(diff_confidence)

    if failed:
        # A storage/vision failure — open a case for a human, never a silent clean pass.
        case = db.insert_damage_case(
            tenant_id, unit_id, booking_id,
            out_report_id=out_report.get("id"), in_report_id=in_report.get("id"),
            diff_findings=findings, diff_confidence=diff_confidence, severity=severity,
            description="Automatic comparison could not complete — please review the "
                        "photos manually.",
            status="open")
        return {"outcome": "manual_review", "damage_case": case, "findings": findings,
                "diff_confidence": diff_confidence, "severity": severity,
                "proposed_deduction": None}

    # A SUGGESTED deduction from the tenant's per-category repair-cost defaults (§12b) — a
    # hint on the case only, never a decision. None when no costs are configured.
    category = (unit or {}).get("category")
    suggested = repair_cost_for(cfg, category, findings)

    high_enough = band == "apply" and SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER["moderate"]
    if findings and high_enough:
        desc = "; ".join(f"{f['area']}: {f['change']} ({f['severity']})" for f in findings)
        case = db.insert_damage_case(
            tenant_id, unit_id, booking_id,
            out_report_id=out_report.get("id"), in_report_id=in_report.get("id"),
            diff_findings=findings, diff_confidence=diff_confidence, severity=severity,
            description=desc, proposed_deduction=suggested, status="open")
        return {"outcome": "damage_claim", "damage_case": case, "findings": findings,
                "diff_confidence": diff_confidence, "severity": severity,
                "proposed_deduction": suggested}

    if findings and band == "review":
        case = db.insert_damage_case(
            tenant_id, unit_id, booking_id,
            out_report_id=out_report.get("id"), in_report_id=in_report.get("id"),
            diff_findings=findings, diff_confidence=diff_confidence, severity=severity,
            description="Possible damage — review the photos before deciding.",
            proposed_deduction=suggested, status="open")
        return {"outcome": "review", "damage_case": case, "findings": findings,
                "diff_confidence": diff_confidence, "severity": severity,
                "proposed_deduction": suggested}

    # <0.55, empty findings, or below-moderate severity → clean return.
    return {"outcome": "clean", "damage_case": None, "findings": findings,
            "diff_confidence": diff_confidence, "severity": "none",
            "proposed_deduction": None}


def damage_facts(unit: dict | None, findings: list[dict], proposed_deduction=None) -> str:
    """Assemble the facts for the "we found X, proposing £Y deduction" draft (§12b) —
    passed to agent.compose_outbound so the tenant-voice claim carries the evidence. Pure.
    The draft is ALWAYS pending (never auto-sent) — the caller forces approval."""
    name = (unit or {}).get("name") or "the hire"
    lines = [f"On return of {name} we found the following that was not present at handover:"]
    for f in findings or []:
        lines.append(f"- {f.get('area')}: {f.get('change')} ({f.get('severity')})")
    if proposed_deduction is not None:
        lines.append(f"We propose a deposit deduction of £{float(proposed_deduction):g} to cover this.")
    lines.append("Photo evidence (handover vs return) is attached for your reference.")
    return "\n".join(lines)


def clean_return_facts(unit: dict | None) -> str:
    """Facts for the clean-return deposit-refund note (§12b) — the happy path draft."""
    name = (unit or {}).get("name") or "the hire"
    return (f"{name} was returned in good condition with no new damage found — "
            "we're releasing the deposit in full. Thank you.")
