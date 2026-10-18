"""Storefront OCR — the wedge: photo of a delivery note → stock lines (docs/os/storefront.md §12.1).

One Claude VISION messages.create (the compose_outbound shape + an image content
block) reads a delivery note / invoice / shelf / chalkboard and returns structured
product lines, each with the model's own confidence. crm_fields.gate (0.75 / 0.5)
then BANDS every line: apply (pre-checked), review (amber, unchecked), skip (shown,
not applied). Nothing writes to stock without the owner's explicit apply tap — the
"read ONLY what's on the document" prompt + the confidence gate + the human tap are
the v1 trust bridge (§12.1). No auto-apply this phase.
"""
import json
import logging

from app import crm_fields, llm, usage
from app.agent import client
from app.config import settings

log = logging.getLogger(__name__)

# The kinds an OCR job can read (mirrors shop_ocr_jobs.kind, §4).
KINDS = ("delivery_note", "invoice", "shelf", "chalkboard")

# Structured-output schema for the vision call (the output_config json_schema seam
# draft_reply/compose_outbound use). One row per product line the model reads.
OCR_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "unit_price": {"type": ["number", "null"]},
                    "best_before": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "qty", "confidence"],
                "additionalProperties": False,
            },
        },
        "document_note": {"type": "string"},
    },
    "required": ["lines"],
    "additionalProperties": False,
}

# The §12.1 prompt VERBATIM — the "read ONLY what's on the document, never invent,
# blurry → LOW confidence" rules are load-bearing (the trust bridge's first half).
_PROMPT = """You are reading a UK shop's {kind} (a delivery note, supplier invoice, shelf, or
chalkboard). Extract every product line you can see. For each line return:
  name          — the product as written (normalise obvious abbreviations)
  qty           — the quantity delivered (a number)
  unit          — each | kg | crate | box | bottle | loaf … (best guess from context)
  unit_price    — price per unit if shown, else null
  best_before   — a date in YYYY-MM-DD if the line or document states one, else null
  confidence    — 0.0-1.0, YOUR certainty this line is correct

Rules:
- Read ONLY what is on the document. Never invent a product, quantity, price or date.
- A blurred or ambiguous line gets LOW confidence, not a guess.
- Totals, VAT lines, addresses and headers are NOT products — skip them.
Return JSON: {{"lines": [ … ], "document_note": "…"}}."""


def _clamp_line(raw: dict) -> dict | None:
    """Validate + coerce one extracted line; drop it (None) if malformed. A line with
    no name or a non-numeric qty is unusable — we never guess it into stock."""
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        qty = float(raw.get("qty"))
    except (TypeError, ValueError):
        return None
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    unit_price = raw.get("unit_price")
    if unit_price not in (None, ""):
        try:
            unit_price = float(unit_price)
        except (TypeError, ValueError):
            unit_price = None
    else:
        unit_price = None
    bb = raw.get("best_before")
    bb = str(bb).strip() if bb not in (None, "") else None
    return {
        "name": name, "qty": qty,
        "unit": (str(raw.get("unit") or "each").strip() or "each"),
        "unit_price": unit_price, "best_before": bb,
        "confidence": conf,
    }


def band_lines(lines: list[dict]) -> list[dict]:
    """Tag each clamped line with crm_fields.gate's verdict (0.75 / 0.5), mapping the
    field-extraction vocabulary onto the review table's bands:
        autofill → 'apply'  (≥0.75, green, pre-checked)
        suggest  → 'review' (0.5–0.75, amber, unchecked — a glance)
        skip     → 'skip'   (<0.5, greyed, shown but not applied)
    Reuses the gate EXACTLY so OCR trust and CRM autofill trust share one threshold."""
    _map = {"autofill": "apply", "suggest": "review", "skip": "skip"}
    out = []
    for ln in lines or []:
        band = _map[crm_fields.gate(ln.get("confidence"))]
        out.append({**ln, "band": band, "checked": band == "apply"})
    return out


async def extract_lines(image_b64: str, kind: str = "delivery_note", *,
                        tenant_id: str | None = None) -> dict:
    """One Claude VISION call → {lines: [banded …], document_note}. Mirrors
    agent.compose_outbound (structured JSON via output_config) with an image content
    block prepended (Anthropic vision). Sonnet (anthropic_booking_model) for reliable
    table reading; metered via usage.record_anthropic(operation='ocr_stock_in').

    GRACEFUL: any failure (bad JSON, API error, no image) returns {"lines": [], …} —
    the route never crashes on a blurry photo or a transient model error."""
    kind = kind if kind in KINDS else "delivery_note"
    prompt = _PROMPT.format(kind=kind.replace("_", " "))
    try:
        msg = await llm.create(
            client=client(), tenant_id=tenant_id, operation="ocr_stock_in", module="storefront",
            model=settings.anthropic_booking_model,
            max_tokens=2000,
            system=prompt,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": "Extract the product lines now."},
            ]}],
            output_config={"format": {"type": "json_schema", "schema": OCR_SCHEMA}},
        )
    except Exception:
        log.warning("OCR extraction call failed for tenant %s", tenant_id, exc_info=True)
        return {"lines": [], "document_note": ""}

    if tenant_id:
        try:
            usage.record_anthropic(tenant_id, msg.model, msg.usage,
                                   operation="ocr_stock_in", module="storefront")
        except Exception:  # metering must never break the extraction
            log.warning("OCR usage metering failed for tenant %s", tenant_id, exc_info=True)

    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("OCR extraction returned non-JSON for tenant %s", tenant_id)
        return {"lines": [], "document_note": ""}

    clamped = [c for c in (_clamp_line(ln) for ln in (data.get("lines") or [])) if c]
    return {
        "lines": band_lines(clamped),
        "document_note": str(data.get("document_note") or "").strip(),
    }
