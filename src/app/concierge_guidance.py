"""Bounded evidence for explaining existing setup choices in text Concierge.

This is context, not an operating tool or a settings recommendation card. Form
options come from the existing Help catalogue; no writes or defaults are applied.
"""
from app import booking_policy, db, people, settings_recommend


# Positive summaries are capability descriptions, not fixed request sentences.
# The field references are the actual settings form contract, not industry menus.
TOPICS = {
    "diary_setup": {
        "description": "Explain diary setup requirements and review opening hours, working days and capacity choices",
        "module": "scheduling", "door": "diary", "page": "diary_settings",
        "fields": ("open_time", "close_time", "working_days", "capacity_per_window"),
        "subjects": "diary setup hours days capacity",
    },
    "booking_spacing": {
        "description": "Compare booking notice, advance booking horizon and gaps between bookings; suggest options for organising the working day",
        "module": "scheduling", "door": "diary", "page": "diary_settings",
        "fields": ("min_notice_hours", "horizon_days", "buffer_minutes"),
        "subjects": "notice horizon gaps spacing bookings",
    },
    "repeat_bookings": {
        "description": "Compare standing repeat bookings with individual bookings and explain repeat frequency choices",
        "module": "scheduling", "door": "diary", "page": "diary_settings",
        "fields": ("recurring_enabled", "recurring_freq", "recurring_max"),
        "subjects": "repeat standing frequency bookings",
    },
    "waiting_lists": {
        "description": "Explain waiting list options and compare approval of offered places with automatic notifications",
        "module": "scheduling", "door": "diary", "page": "diary_settings",
        "fields": ("waitlist_on", "waitlist_notify", "offer_hours"),
        "subjects": "waiting list places notifications",
    },
    "customer_organisation": {
        "description": "Suggest customer pipeline stages and quiet contact thresholds to organise follow-up work",
        "module": "crm", "door": "crm", "page": "crm_settings",
        "fields": ("stage", "quiet_days"),
        "subjects": "pipeline stages thresholds follow customer",
    },
    "payment_terms": {
        "description": "Help review invoice payment terms and quote validity options for the business",
        "module": None, "door": "money", "page": "invoice_settings",
        "fields": ("payment_terms", "default_quote_valid_days"),
        "subjects": "payment terms validity quote",
    },
    "deposit_choices": {
        "description": "Compare deposit choices on quote acceptance and booking confirmation; explain when deposits are requested",
        "module": None, "door": "money", "page": "invoice_settings",
        "fields": ("default_deposit_percent", "deposit_on_confirm"),
        "subjects": "deposit deposits acceptance confirmation",
    },
    "prepaid_packages": {
        "description": "Compare prepaid packages of sessions or hours with paying per booking and explain credit options",
        "module": "scheduling", "door": "money", "page": "packages",
        "fields": ("package", "packages_paid_only"),
        "subjects": "packages prepaid credits credit",
    },
}


def _value(value):
    if isinstance(value, str):
        return value[:400]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v[:32] for v in value[:8]]
    return None


def context(ctx, member, modules, *, cfg=None, tenant_config=None):
    """Fresh authorised settings evidence; absent values stay unknown/unset.

    Settings access AND the relevant module/door are required. No private
    record fields, mailbox credentials, invoice identities or corpora are read.
    """
    if not people.can_open(member, "settings"):
        return {}
    topics = {key: spec for key, spec in TOPICS.items()
              if (not spec["module"] or spec["module"] in modules)
              and people.can_open(member, spec["door"])
              and (key != "prepaid_packages" or people.can_open(member, "diary"))}
    if not topics:
        return {}
    cfg = (db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}) if cfg is None else cfg
    tenant_config = (db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}) if tenant_config is None else tenant_config
    invoices = (db.get_invoice_settings(ctx.tenant_id, client=ctx.client) or {}) if any(
        spec["page"] == "invoice_settings" for spec in topics.values()) else {}
    recurring = cfg.get("recurring") if isinstance(cfg.get("recurring"), dict) else {}
    courses = cfg.get("courses") if isinstance(cfg.get("courses"), dict) else {}
    values = {
        "diary_setup": {key: cfg.get(key) for key in TOPICS["diary_setup"]["fields"]},
        "booking_spacing": {key: cfg.get(key) for key in TOPICS["booking_spacing"]["fields"]},
        "repeat_bookings": {"recurring_enabled": recurring.get("enabled"),
                            "recurring_freq": recurring.get("frequencies"), "recurring_max": recurring.get("max_occurrences")},
        "waiting_lists": {"waitlist_on": courses.get("waitlist"), "waitlist_notify": courses.get("waitlist_notify"),
                          "offer_hours": courses.get("offer_hours")},
        "customer_organisation": {"quiet_days": tenant_config.get("crm_quiet_days")},
        "payment_terms": {key: invoices.get(key) for key in TOPICS["payment_terms"]["fields"]},
        "deposit_choices": {key: invoices.get(key) for key in TOPICS["deposit_choices"]["fields"]},
        "prepaid_packages": {},
    }
    result = {}
    for key, spec in topics.items():
        page = settings_recommend.PAGES.get(spec["page"], {})
        fields = page.get("fields", {})
        # Removed/oversized catalogue entries close discovery until reviewed.
        if any(field not in fields or len(fields[field]) > 1000 for field in spec["fields"]):
            continue
        result[key] = {"description": spec["description"], "page_label": page["label"],
                       "options": {field: fields[field] for field in spec["fields"]},
                       "saved_values": {field: _value(value) for field, value in values[key].items()}}
        if key == "diary_setup":
            result[key]["unresolved_settings"] = booking_policy.compile_profile(cfg).as_policy()["unresolved_settings"]
    return result
