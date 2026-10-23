"""Post-onboarding guided-settings hand-off (docs/modules/module-concierge.md).

The wizard's Finish is not the end of setup: the settings that make the agent
genuinely useful — how the diary books, the CRM pipeline, the business + bank
details on invoices — are tuned in-app, and a freshly-onboarded tenant lands on
/app with no signpost. This module is the WHEN and the WHAT of the concierge
widget's continuation: should the guided strip show for this tenant, and which
settings steps remain, in order (diary → CRM → invoicing, the order the money
flows).

Pure decisions over rows the route reads — nothing here writes. Zero user
initiative (Julius, live e2e 2026-08-02): the widget announces the NEXT step
unprompted — right tab, panel open, a next-step message with a nav link — and
every step affordance NAVIGATES to its settings page, where the existing
on-page flows take over ("Help me set this up" → the "Use recommended
settings" card, values pre-filled for review, saving stays each form's own
Save; invoice details are facts only the owner knows — nothing to recommend).

Exit paths: every remaining step gets configured (derived live from the
settings rows), the tenant explicitly dismisses (tenant_config
.settings_handoff_dismissed_at), or the freshness window lapses — the strip is
a hand-off, not a permanent nag.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re


# How long after onboarding the hand-off stays offered. Long enough for the
# "I'll do it at the weekend" owner, short enough that a long-established
# tenant who never wanted it stops being nudged.
FRESH_DAYS = 14

# The diary-backed modules (the same set app/concierge_routes._tenant_context
# uses for its diary_configured flag).
DIARY_MODULES = {"scheduling", "tms", "fsm"}


def is_fresh(onboarded_at, now: datetime | None = None) -> bool:
    """Within the hand-off window of finishing onboarding. Missing/garbage
    timestamps are NOT fresh — a tenant we can't date shouldn't be nudged."""
    try:
        done = datetime.fromisoformat(str(onboarded_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if done.tzinfo is None:
        done = done.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - done <= timedelta(days=FRESH_DAYS)


# tenant_config.crm_quiet_days is NOT NULL DEFAULT 14 (db/schema.sql) — every
# tenant row carries 14 untouched, so only a DIFFERENT value proves the tenant
# actually set the threshold.
DEFAULT_CRM_QUIET_DAYS = 14


def crm_configured(stages: list[dict] | None, quiet_days) -> bool:
    """Has the tenant made the CRM their own? Since 2026-09-02 nothing seeds
    stages — every stage was recommended-and-applied or hand-added — so ANY
    stage existing means configured, as does a gone-quiet threshold changed
    from its default."""
    try:
        if quiet_days and int(quiet_days) != DEFAULT_CRM_QUIET_DAYS:
            return True
    except (TypeError, ValueError):
        pass
    return any(isinstance(s, dict) and (s.get("name") or "").strip()
               for s in stages or [])


def invoice_configured(row: dict | None) -> bool:
    """Invoice details count as filled in once the identity or payment half is
    there: a business name, or bank-transfer details (the e2e standing gap)."""
    row = row or {}
    return bool((row.get("business_name") or "").strip()
                or (row.get("bank_account_number") or "").strip())


def steps(modules: set, scheduling_cfg: dict | None, crm_stages: list | None,
          crm_quiet_days, invoice_row: dict | None,
          dependant_groups: list | None = None, *, review_state=None) -> list[dict]:
    """The tenant's walk-through, in order, with per-step done flags. Steps
    whose module isn't on are omitted entirely (invoicing is core — always
    present). Diary setup is done when booking is ready OR the owner explicitly
    disabled it; a confirmed off choice is complete setup, but never grants
    booking permission."""
    if review_state is not None:
        from app.onboarding_contract import group_steps
        return group_steps(modules, scheduling_cfg, review_state, dependant_groups)
    # Compatibility for legacy callers. Current application reads supply review
    # metadata; these old heuristics never approve new groups.
    out = []
    if DIARY_MODULES & modules:
        from app.booking_policy import compile_profile
        booking_profile = compile_profile(scheduling_cfg)
        booking_ready = booking_profile.ready
        booking_setup_complete = booking_profile.setup_complete
        out.append({"key": "diary", "label": "Diary & scheduling settings",
                    "done": booking_setup_complete, "href": "/app/diary/settings",
                    "status": ("" if booking_ready else
                               ("Bookings explicitly disabled" if booking_setup_complete else
                                "Not set yet: booking unavailable"))})
        # Step 1.5 (Julius, live e2e 2026-09-02): the booking requirements are
        # the heart of the booking promise and were not in the walk-through at
        # all — they follow the diary (they describe what the diary's bookings
        # must collect) and precede the CRM. Done = the intake save's stamp.
        out.append({"key": "requirements", "label": "Booking requirements",
                    "done": bool((scheduling_cfg or {}).get("intake_configured_at")),
                    "href": "/app/diary/intake"})
        # Step 2.5 (Kris, 2026-09-04): what a booking is ABOUT — the customer's
        # subjects and assets — is its own settings page with its own auto-ask;
        # the walk used to skip it. Done = a kind exists, OR the page was
        # answered (run #8, Kris: "if no kind applies it shouldn't be offered
        # as a step" — a ladder-only save, an empty save, or the assistant's
        # own card saying there is nothing of the sort all stamp it).
        out.append({"key": "dependants", "label": "Dependants",
                    "done": bool(dependant_groups)
                    or bool((scheduling_cfg or {}).get("dependants_configured_at")),
                    "href": "/app/diary/dependants"})
    if "crm" in modules:
        # CRM settings has no Save of its own — the pipeline is per-row forms
        # and the assistant's card writes on press. "Save this page to move on"
        # sent the owner hunting for a button that isn't there (run #10 (iii)).
        out.append({"key": "crm", "label": "CRM settings",
                    "done": crm_configured(crm_stages, crm_quiet_days),
                    "move_on": "Set a pipeline to move on",
                    "href": "/app/crm/settings"})
    # What finishes it, said on the strip (Kris 2026-09-25, rehearsal: a Save
    # with no name or bank details left the step undone and gave no reason).
    out.append({"key": "invoicing", "label": "Getting paid",
                "done": invoice_configured(invoice_row),
                "move_on": "Add your business name or bank details, then Save",
                "href": "/app/invoices/settings"})
    return out


def show(onboarded_at, dismissed_at, step_list: list[dict],
         now: datetime | None = None) -> bool:
    """The whole gate: fresh, never dismissed, and something left to tune."""
    if dismissed_at or not is_fresh(onboarded_at, now=now):
        return False
    return any(not s["done"] for s in step_list)


# ── One flow of nine (Kris, 2026-09-13) ──────────────────────────────────────
# The wizard's four steps (About you, Customer inbox, Voice, Knowledge) and the
# tuning steps above are ONE setup: the wizard's last button says "Next: <first
# tuning step>", the app shell carries a strip until every step is done, and
# the "done" moment comes at the end — not after step four.
WIZARD_STEPS = 4


def load_steps(tenant_id: str, client=None) -> list[dict]:
    """The tenant's tuning steps with live done flags — the same reads as
    /concierge/setup-status, for the shell strip and the wizard's Next.
    Best-effort: [] on any failure (a strip is never worth a 500)."""
    from app import db, parallel_reads
    try:
        modules = db.active_modules(tenant_id, client=client)
        # The five reads after the module set do not depend on one another: one round trip of
        # waiting, not five (each is ~45-110 ms from Render to Supabase).
        wanted_stages = "crm" in modules
        wanted_groups = bool(DIARY_MODULES & set(modules))
        calls = [lambda: db.get_scheduling_config(tenant_id, client=client),
                 lambda: db.get_invoice_settings(tenant_id, client=client),
                 lambda: db.get_tenant_config(tenant_id, client=client) or {}]
        if wanted_stages:
            calls.append(lambda: db.crm_list_stages(tenant_id, client=client))
        if wanted_groups:
            calls.append(lambda: db.list_requirement_groups(tenant_id, client=client))
        results = parallel_reads.run_all(calls)
        cfg, invoice_row, tcfg = results[0], results[1], results[2]
        rest = iter(results[3:])
        stages = next(rest) if wanted_stages else []
        groups = next(rest) if wanted_groups else []
        return steps(set(modules), cfg, stages, tcfg.get("crm_quiet_days"), invoice_row,
                     dependant_groups=groups, review_state=tcfg.get('onboarding_state') or {})
    except Exception:
        return []


def first_open(step_list: list[dict]) -> dict | None:
    return next((s for s in step_list if not s.get("done")), None)


def _progress_name(label: str | None) -> str:
    """A step's name in the progress display: its page title without a trailing
    "settings" ("Diary & scheduling settings" -> "Diary & scheduling")."""
    label = (label or "").strip()
    return re.sub(r"\s+settings$", "", label, flags=re.IGNORECASE) or label


def strip(step_list: list[dict], current_path: str | None = None,
          onboarded_at=None, dismissed_at=None, now: datetime | None = None) -> dict | None:
    """The shell strip: where the owner is in the nine, what is next, and
    whether they are standing on a step's page. None once everything is done
    or the walk was hidden. A fresh tenant (first FRESH_DAYS) cannot hide it —
    Kris: the five in-app steps must not be one tap from vanishing; an older
    tenant may (`can_hide`), which also spares long-standing accounts a strip
    they never asked for."""
    if not step_list or dismissed_at:
        return None
    nxt = first_open(step_list)
    if not nxt:
        return None
    total = WIZARD_STEPS + len(step_list)
    done = WIZARD_STEPS + sum(1 for s in step_list if s.get("done"))
    here = next((s for s in step_list if s.get("href") == current_path or current_path in s.get('aliases', [])), None) if current_path else None
    pos = after = None
    if here:
        idx = step_list.index(here)
        pos = WIZARD_STEPS + idx + 1
        after = next((s for s in step_list[idx + 1:] if not s.get("done")), None)
    # The in-app steps as the shell's numbered progress (the wizard's four are behind
    # the owner by now and are not drawn). The badge is the circle's text: a step may
    # carry its own ("5A"); otherwise it is its place in the nine. Focus is the page
    # the owner stands on, else the next open step.
    focus = here or nxt
    items = [{"key": s.get("key"), "badge": str(s.get("badge") or WIZARD_STEPS + i + 1),
              "name": _progress_name(s.get("label")), "done": bool(s.get("done")), "current": s is focus}
             for i, s in enumerate(step_list)]
    return {"total": total, "done": done, "next": nxt, "here": here, "pos": pos, "after": after,
            "here_done": bool(here and here.get("done")), "items": items,
            "can_hide": not is_fresh(onboarded_at, now=now)}


# settings_recommend page → the tuning step that owns it (customer fields
# has no step; knowledge is the wizard's own step and never a tuning one).
PAGE_STEPS = {"diary_settings": "diary", "booking_details": "requirements",
              "dependants": "dependants", "crm_settings": "crm",
              "invoice_settings": "invoicing"}


def nav_allowed(link: dict | None, step_list: list[dict], path: str | None) -> bool:
    """Whether a help reply's navigation bubble may show. While the setup walk
    is unfinished, a link to ANOTHER step's page is a door out of the step the
    owner is standing on — run #10 (ccc): a stray "Open Booking details →" sat
    above the Dependants card and pointed a step backwards. Links to anywhere
    that is not a setup step (the Knowledge page, a diary day) are untouched,
    and once every step is done nothing is filtered at all."""
    href = str((link or {}).get("href") or "").split("?")[0].split("#")[0]
    if not href or not step_list or not first_open(step_list):
        return True
    hrefs = {str(p).split('#')[0] for s in step_list for p in [s.get('href') or '', *s.get('aliases', [])]}
    here = (path or "").split("?")[0].split("#")[0]
    return not (href in hrefs and href != here)


def _page_href(page: str) -> str:
    from app import settings_recommend
    href = (settings_recommend.PAGES.get(page) or {}).get("href") or ""
    return href.split("#")[0]


def cards_for(recs: list | None, step_list: list[dict] | None, path: str | None,
              auto: bool) -> list[dict]:
    """The recommend cards a help reply may carry while the walk is on.

    Re-run 2026-09-13 (step 9, Invoice details): the page's silent arrival
    ask came back with five cards for pages already done — diary, booking
    requirements, dependants, CRM and four knowledge notes already saved —
    each a "press it" that would refill or replace a finished page. While
    any tuning step is undone: an automatic (arrival) ask keeps only cards
    for the page it is on; a typed question keeps a card for its own page or
    for any step not yet done. Past the walk everything flows as before."""
    recs = [r for r in (recs or []) if isinstance(r, dict) and r.get("page")]
    steps = {s.get("key"): s for s in (step_list or []) if s.get("key")}
    if not recs or not steps or all(s.get("done") for s in steps.values()):
        return recs
    path = (path or "").split("?")[0].split("#")[0]
    out = []
    for r in recs:
        on_page = bool(path) and _page_href(r["page"]) == path
        if auto:
            if on_page:
                out.append(r)
            continue
        step = steps.get(PAGE_STEPS.get(r["page"], ""))
        if any(s.get('substeps') for s in steps.values()):
            from app.onboarding_contract import PAGE_SUBSTEPS
            sid = PAGE_SUBSTEPS.get(r['page'])
            step = next((sub for group in steps.values() for sub in group.get('substeps', []) if sub['id'] == sid), None)
        if on_page or not step or not step.get("done"):
            out.append(r)
    return out
