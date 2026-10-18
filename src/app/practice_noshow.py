"""Practice OS no-show prediction — the wedge's second half (docs/os/practice.md §12.1).

Two stages, both cheap and explainable:

1. `heuristic_prior(features)` — a DETERMINISTIC weighted sum (0–100) of the behavioural
   features assembled by db.contact_no_show_history + the booking row. Prior no-show RATE
   is the strongest term; the rest are smaller nudges. This is computed first and passed
   INTO the LLM prompt so the model refines a stable number rather than hallucinating one.

2. `score_appointment(cfg, features, prior)` — a structured-output LLM refine on the Haiku
   model BLAM8 already meters (settings.anthropic_model), mirroring agent.compose_outbound's
   metered call. Returns {score, band, reason}; the band is always re-derived from the
   tenant's thresholds (practice_config.band_for_score) so the model's own band label can
   never disagree with the practice's configured cut-offs.

COLD START (§14, the guardrail that stops us crying wolf). A new patient / new tenant with
no attendance history must band CONSERVATIVELY — never `high` on zero signal. With no prior
appointments the prior is CAPPED below the high threshold (so at most `medium`), and the
LLM is told the same in-prompt. Risk is earned by behaviour, not assumed on a first visit.

PRIVACY (§12.1). The scorer sees behavioural metadata ONLY — counts, latencies, lead time,
day/time, service name/paid flag. Never a clinical note or allergy: contact_no_show_history
returns numbers, not note text, and nothing here reads practice_medical_notes.
"""
import json
import logging

from app import llm, practice_config, usage
from app.config import settings

log = logging.getLogger(__name__)


# ── the deterministic prior ──────────────────────────────────────────────────
# Weighted sum, capped to 0–100. The weights below are the design; each is documented
# inline. The BASELINE is a low floor (a typical patient is unlikely to no-show), and the
# strongest positive term is the contact's own prior no-show RATE — the single best
# predictor (§12.1). Everything else is a smaller nudge up or down.
BASELINE = 12                    # a fresh, no-signal appointment starts low, not at zero

# The most a NO-history appointment can reach from booking-pattern priors alone. Held below
# the default `high` cut-off (70) so cold-start can never band `high` (§14 guardrail).
COLD_START_CAP = 55

# History weights (dominant term).
_W_PRIOR_RATE = 60               # no_shows/total_appts × this — a 50% history ≈ +30
_W_PRIOR_RECENT = 8              # a no-show on record at all (recency proxy) — a steady bump

# Booking-pattern weights (each a modest nudge; §12.1).
_W_LONG_LEAD = 12                # booked far ahead → more time to forget (>21 days)
_W_MED_LEAD = 5                  # 8–21 days ahead → mild
_W_EARLY_MORNING = 6            # first slot of the day skews higher
_W_LATE_FRIDAY = 7              # late Friday skews higher (weekend drift)
_W_FIRST_VISIT = 6             # a first appointment, no relationship yet
_W_RESCHEDULED = 5             # per prior reschedule (capped) — churny booking
_W_FREE_SERVICE = 8           # a free/unpriced service no-shows more than a paid one

# Engagement weights.
_W_SLOW_REPLY = 8             # avg reply latency > 48h → disengaged
_W_REMINDERS_IGNORED = 12     # we've chased and heard nothing back
_W_CONFIRM_CLICK = -30        # a confirm-link click / reply sharply LOWERS risk (§12.1)


def heuristic_prior(features: dict) -> int:
    """A deterministic 0–100 prior from the §12.1 features. Higher = more likely to miss.

    features (all optional, safe defaults):
      total_appts, no_shows, has_prior_no_show, lead_days, weekday (0=Mon..6=Sun),
      hour (0–23), first_visit, rescheduled, service_paid, avg_reply_latency_hours,
      reminders_ignored, confirmed_click.

    Cold start: when total_appts == 0 the score is capped at COLD_START_CAP (below the high
    threshold), so a brand-new patient/tenant never bands `high` on pattern alone (§14)."""
    f = features or {}
    score = float(BASELINE)

    total = int(f.get("total_appts") or 0)
    no_shows = int(f.get("no_shows") or 0)
    if total > 0:
        rate = max(0.0, min(1.0, no_shows / total))
        score += _W_PRIOR_RATE * rate
    if f.get("has_prior_no_show") or no_shows > 0:
        score += _W_PRIOR_RECENT

    lead = f.get("lead_days")
    if lead is not None:
        if lead > 21:
            score += _W_LONG_LEAD
        elif lead >= 8:
            score += _W_MED_LEAD

    hour = f.get("hour")
    weekday = f.get("weekday")
    if hour is not None and hour <= 8:
        score += _W_EARLY_MORNING
    if weekday == 4 and hour is not None and hour >= 15:   # Friday afternoon
        score += _W_LATE_FRIDAY

    if f.get("first_visit"):
        score += _W_FIRST_VISIT
    resched = int(f.get("rescheduled") or 0)
    if resched:
        score += min(resched, 3) * _W_RESCHEDULED
    if f.get("service_paid") is False:
        score += _W_FREE_SERVICE

    latency = f.get("avg_reply_latency_hours")
    if latency is not None and latency > 48:
        score += _W_SLOW_REPLY
    if f.get("reminders_ignored"):
        score += _W_REMINDERS_IGNORED
    if f.get("confirmed_click"):
        score += _W_CONFIRM_CLICK

    score = max(0, min(100, round(score)))
    if total == 0:
        score = min(score, COLD_START_CAP)      # cold-start guardrail (§14)
    return int(score)


# ── the LLM refine ───────────────────────────────────────────────────────────
NO_SHOW_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "band": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["score", "band", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You score how likely a patient is to miss an appointment, 0–100, and give a one-line "
    "reason a receptionist would understand. You are given behavioural features and a "
    "heuristic prior score. Adjust the prior using judgement about the pattern; do NOT "
    "invent facts that are not in the features, and do NOT reference any medical or clinical "
    "detail (you are given none — only booking behaviour). If the patient has NO prior "
    "appointment history, stay conservative: do not call a first-time, no-signal patient "
    "high-risk. Return {\"score\":int (0-100),\"band\":\"low|medium|high\",\"reason\":str} "
    "and nothing else."
)


def _features_block(features: dict, prior: int) -> str:
    """The §12.1 features prompt — behavioural metadata only, formatted for the model."""
    f = features or {}
    total = int(f.get("total_appts") or 0)
    no_shows = int(f.get("no_shows") or 0)
    last = f.get("last_no_show_at") or "none"
    lead = f.get("lead_days")
    weekday = f.get("weekday_label") or "?"
    time = f.get("time_label") or "?"
    lat = f.get("avg_reply_latency_hours")
    lines = [
        f"- prior appointments: {total}, no-shows: {no_shows} (last {last})",
        f"- booked {lead if lead is not None else '?'} days ahead, {weekday} {time}, "
        f"first_visit={bool(f.get('first_visit'))}, rescheduled={int(f.get('rescheduled') or 0)}",
        f"- avg reply latency: {lat if lat is not None else 'n/a'}h, "
        f"reminders_ignored={bool(f.get('reminders_ignored'))}, "
        f"confirmed_click={bool(f.get('confirmed_click'))}",
        f"- service: {f.get('service_name') or 'appointment'}, paid={bool(f.get('service_paid'))}",
        f"Heuristic prior score: {prior}",
    ]
    if total == 0:
        lines.append("Note: NO prior history for this patient — stay conservative (cold start).")
    return "\n".join(lines)


async def score_appointment(cfg: dict, features: dict, prior: int,
                            tenant_id: str | None = None,
                            email_log_id: str | None = None) -> dict:
    """Refine the heuristic prior with one metered Haiku call and return
    {"score":int, "band":"low|medium|high", "reason":str}. Mirrors compose_outbound's
    structured-output + usage.record_anthropic pattern. The band is ALWAYS re-derived from
    the tenant's thresholds (band_for_score), so a disagreeing model label can't leak.

    On any parse/validation failure we fall back to the deterministic prior — the prior is
    always a usable answer, so the scorer degrades gracefully rather than crashing a sweep.

    `cfg` carries the tenant voice/scheduling_config; scheduling_config supplies thresholds.
    Passing tenant_id enables metering (skipped when None, e.g. a pure unit test)."""
    from app import agent   # local import: avoid a circular import at module load
    system = _SYSTEM + "\n\nFeatures:\n" + _features_block(features, prior)
    sched_cfg = (cfg or {}).get("scheduling_config") or (cfg or {})
    try:
        msg = await llm.create(
            client=agent.client(), tenant_id=tenant_id, operation="no_show_score",
            model=settings.anthropic_model,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": "Score this appointment now."}],
            output_config={"format": {"type": "json_schema", "schema": NO_SHOW_SCHEMA}},
        )
        if tenant_id:
            usage.record_anthropic(tenant_id, msg.model, msg.usage,
                                   operation="no_show_score", email_log_id=email_log_id)
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", None) == "text").strip()
        parsed = json.loads(text)
        score = int(parsed["score"])
    except Exception:
        log.warning("no-show LLM refine failed; falling back to heuristic prior", exc_info=True)
        score = int(prior)
        reason = _fallback_reason(features, score)
        return {"score": score, "band": practice_config.band_for_score(score, sched_cfg),
                "reason": reason}

    score = max(0, min(100, score))
    reason = str(parsed.get("reason") or "").strip()[:280] or _fallback_reason(features, score)
    band = practice_config.band_for_score(score, sched_cfg)   # tenant thresholds win
    return {"score": score, "band": band, "reason": reason}


def _fallback_reason(features: dict, score: int) -> str:
    """A deterministic one-liner when the model gives none — built from the same features
    (a receptionist-readable summary), so the panel always shows a why."""
    f = features or {}
    bits = []
    no_shows = int(f.get("no_shows") or 0)
    total = int(f.get("total_appts") or 0)
    if no_shows:
        bits.append(f"{no_shows} prior no-show{'s' if no_shows != 1 else ''}")
    elif total == 0:
        bits.append("new patient, no history yet")
    if f.get("reminders_ignored"):
        bits.append("hasn't replied to reminders")
    lead = f.get("lead_days")
    if lead is not None and lead > 21:
        bits.append(f"booked {lead} days out")
    if f.get("first_visit") and total:
        bits.append("first visit")
    if not bits:
        bits.append("no strong risk signals")
    return ", ".join(bits[:3])
