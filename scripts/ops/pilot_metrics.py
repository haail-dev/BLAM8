"""Per-tenant pilot metrics — the case-study numbers, from data already captured.

The AI-recommendability plan (docs/ai-recommendability-plan.md §P2) needs real,
dated numbers before a single case study can be written. Everything below is
derived from what the product already records, and every input survives the
90-day redaction job (only email/draft TEXT is wiped; flags, timestamps, links
and CRM rows stay):

  reply time     email_log.created_at (ingest) -> the agent_events kind='sent'
                 row joined on email_log_id. Ingest time, not the customer's
                 send time — with push + SSE the gap is seconds, but say
                 "measured from arrival in BLAM8" in any published number.
  draft quality  db.email_log_quality: approval %, edited vs verbatim splits
  automation     email_log.sent_via 'auto' vs 'human'
  bookings       bookings.source — 'email' = the agent booked it from the
                 conversation, 'manual' = someone typed it into the UI
  CRM autofill   crm_contacts / companies / deals / promises created in-window
                 (the CRM only fills from the mail flow, so counts ≈ autofill)
  mood           crm_activities.sentiment reads that flagged concern or worse,
                 plus contacts currently sitting flagged
  never-rules    tenant_config.never_do line count (thumbs-down corrections)

Read-only, service role (an explicit operational command):

    python scripts/ops/pilot_metrics.py                     # every tenant, last 30 days
    python scripts/ops/pilot_metrics.py 2026-07-01          # window from a date (pilot start)
    python scripts/ops/pilot_metrics.py 2026-07-01 <tenant-uuid>
"""
import sys
from datetime import datetime, timedelta, timezone

from app import db

# Windows consoles default to cp1252, which can't print the report's glyphs
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FLAGGED = ("concerned", "frustrated", "angry")


def _paged(query_fn) -> list[dict]:
    """Page past the 1000-row PostgREST cap; query_fn(offset) returns one page."""
    rows: list[dict] = []
    while True:
        page = query_fn(len(rows)) or []
        rows.extend(page)
        if len(page) < 1000:
            return rows


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "–"


def _fmt_secs(s: float | None) -> str:
    if s is None:
        return "–"
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _count(table: str, tenant_id: str, since_iso: str, **eqs) -> int:
    q = (db.db().table(table).select("id", count="exact", head=True)
         .eq("tenant_id", tenant_id).gte("created_at", since_iso))
    for col, val in eqs.items():
        q = q.eq(col, val)
    return (q.execute().count) or 0


def tenant_metrics(tenant_id: str, since_iso: str) -> dict:
    c = db.db()

    inbound = _paged(lambda off: (
        c.table("email_log").select("id,status,kind,thread_id,from_address,created_at")
        .eq("tenant_id", tenant_id).gte("created_at", since_iso)
        .order("created_at").range(off, off + 999).execute().data))
    # kind was added later; legacy NULL rows are inbound (the column's default)
    inbound = [r for r in inbound if (r.get("kind") or "inbound") == "inbound"]

    sent_events = _paged(lambda off: (
        c.table("agent_events").select("email_log_id,created_at")
        .eq("tenant_id", tenant_id).eq("kind", "sent")
        .gte("created_at", since_iso).not_.is_("email_log_id", "null")
        .order("created_at").range(off, off + 999).execute().data))

    # Reply time: first 'sent' event per inbound email. ISO parse tolerates the
    # +00:00 / Z variants Supabase emits.
    def _ts(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    arrived = {r["id"]: _ts(r["created_at"]) for r in inbound}
    first_sent: dict[str, datetime] = {}
    for ev in sent_events:
        lid = ev["email_log_id"]
        if lid in arrived and lid not in first_sent:
            first_sent[lid] = _ts(ev["created_at"])
    deltas = sorted((first_sent[lid] - arrived[lid]).total_seconds()
                    for lid in first_sent
                    if (first_sent[lid] - arrived[lid]).total_seconds() >= 0)

    quality = db.email_log_quality(tenant_id, since_iso)

    bookings = _paged(lambda off: (
        c.table("bookings").select("id,source,status")
        .eq("tenant_id", tenant_id).gte("created_at", since_iso)
        .order("created_at").range(off, off + 999).execute().data))
    real = [b for b in bookings if b.get("status") not in ("declined", "cancelled")]
    agent_booked = sum(1 for b in real if b.get("source") == "email")

    mood_reads = (c.table("crm_activities").select("id", count="exact", head=True)
                  .eq("tenant_id", tenant_id).gte("created_at", since_iso)
                  .in_("sentiment", list(FLAGGED)).execute().count) or 0
    flagged_now = (c.table("crm_contacts").select("id", count="exact", head=True)
                   .eq("tenant_id", tenant_id)
                   .in_("sentiment", list(FLAGGED)).execute().count) or 0

    cfg = (c.table("tenant_config").select("never_do")
           .eq("tenant_id", tenant_id).limit(1).execute().data or [{}])[0]
    never_rules = len([ln for ln in (cfg.get("never_do") or "").splitlines() if ln.strip()])

    days = max(1, (datetime.now(timezone.utc) - _ts(since_iso)).days)
    return {
        "window_days": days,
        "inbound": len(inbound),
        "inbound_per_week": 7 * len(inbound) / days,
        "threads": len({r["thread_id"] for r in inbound if r.get("thread_id")}),
        "senders": len({r["from_address"] for r in inbound if r.get("from_address")}),
        "reply_median_s": _quantile(deltas, 0.5),
        "reply_p90_s": _quantile(deltas, 0.9),
        "replies_timed": len(deltas),
        "quality": quality,
        "bookings": len(real),
        "agent_booked": agent_booked,
        "crm_contacts": _count("crm_contacts", tenant_id, since_iso),
        "crm_companies": _count("crm_companies", tenant_id, since_iso),
        "crm_deals": _count("crm_deals", tenant_id, since_iso),
        "crm_promises": _count("crm_promises", tenant_id, since_iso),
        "mood_reads_flagged": mood_reads,
        "contacts_flagged_now": flagged_now,
        "never_rules": never_rules,
    }


def print_report(name: str, tenant_id: str, m: dict, since_iso: str) -> None:
    q = m["quality"]
    print(f"\n━━ {name}  ({tenant_id})")
    print(f"   window: since {since_iso[:10]} ({m['window_days']}d)")
    print(f"   volume: {m['inbound']} inbound handled "
          f"({m['inbound_per_week']:.0f}/wk) · {m['threads']} threads · "
          f"{m['senders']} unique senders")
    approval = f"{q['approval_pct']:.0f}%" if q["approval_pct"] is not None else "–"
    print(f"   replies: {q['sent']} sent ({q['auto_sent']} auto / {q['human_sent']} approved) · "
          f"approval {approval} · "
          f"sent verbatim {_pct(q['verbatim'], q['verbatim'] + q['edited'])}")
    print(f"   reply time (arrival→sent, n={m['replies_timed']}): "
          f"median {_fmt_secs(m['reply_median_s'])} · p90 {_fmt_secs(m['reply_p90_s'])}")
    print(f"   bookings: {m['bookings']} · booked by the agent from email: "
          f"{m['agent_booked']} ({_pct(m['agent_booked'], m['bookings'])})")
    print(f"   CRM self-filled in window: {m['crm_contacts']} contacts · "
          f"{m['crm_companies']} companies · {m['crm_deals']} deals · "
          f"{m['crm_promises']} promises")
    print(f"   mood: {m['mood_reads_flagged']} flagged reads in window · "
          f"{m['contacts_flagged_now']} contacts currently flagged · "
          f"{m['never_rules']} standing never-rules")


def main() -> int:
    args = sys.argv[1:]
    since = (args[0] if args else
             (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"))
    since_iso = f"{since}T00:00:00+00:00"
    only_tenant = args[1] if len(args) > 1 else None

    tenants = db.list_all_tenants()
    if only_tenant:
        tenants = [t for t in tenants if t["id"] == only_tenant]
        if not tenants:
            print(f"No tenant {only_tenant}")
            return 1

    print(f"Pilot metrics since {since} — case-study inputs "
          f"(docs/ai-recommendability-plan.md §P2, publish only with consent)")
    for t in tenants:
        try:
            m = tenant_metrics(t["id"], since_iso)
        except Exception as e:   # one broken tenant must not kill the sweep
            print(f"\n━━ {t.get('name') or t['id']}: FAILED — {e}")
            continue
        if not m["inbound"] and not m["bookings"]:
            print(f"\n━━ {t.get('name') or t['id']}  ({t['id']}): no activity in window")
            continue
        print_report(t.get("name") or "(unnamed)", t["id"], m, since_iso)
    print("\nCaveats: reply time is measured from arrival in BLAM8 (ingest), not the "
          "customer's send time; 'sent verbatim' covers human-approved sends with the "
          "edit flag stamped (post 2026-06 rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
