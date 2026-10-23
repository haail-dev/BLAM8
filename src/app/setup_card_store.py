"""Strict reads and one compare-and-swap write; no best-effort empty fallbacks."""
from app import db, setup_cards


def load_all(tenant_id, *, client=None):
    config = db.get_tenant_config(tenant_id, client=client)
    return (config or {}).get("setup_cards", {})


def load(tenant_id, page, *, client=None):
    if page not in setup_cards.PAGES:
        raise setup_cards.Invalid("Unknown setup card page.")
    config = db.get_tenant_config(tenant_id, client=client)
    return (config or {}).get("setup_cards", {}).get(page)


def history(tenant_id, page, *, before=None, client=None):
    if page not in setup_cards.PAGES:
        raise setup_cards.Invalid("Unknown setup card page.")
    query = ((client or db.db()).table("setup_card_revisions").select("revision,card,created_at")
             .eq("tenant_id", tenant_id).eq("page", page))
    if before is not None:
        query = query.lt("revision", before)
    return query.order("revision", desc=True).limit(50).execute().data


def latest_receipt(tenant_id, page, card_id, *, client=None):
    """A card can be terminal and archived while an onboarding receipt is pending."""
    if page not in setup_cards.PAGES:
        raise setup_cards.Invalid('Unknown setup card page.')
    rows = ((client or db.db()).table('setup_card_revisions').select('revision,card')
            .eq('tenant_id', tenant_id).eq('page', page).eq('card->>id', card_id)
            .order('revision', desc=True).limit(1).execute().data)
    return rows[0]['card'] if rows else None


def save(tenant_id, page, revision, card, *, expected_settings=None, settings_patch=None, client=None):
    try:
        response = (client or db.db()).rpc("change_setup_card", {
            "p_tenant": tenant_id, "p_page": page, "p_revision": revision, "p_card": card,
            "p_expected_settings": expected_settings, "p_settings_patch": settings_patch,
        }).execute()
    except Exception as exc:
        if any(code in str(exc) for code in ("BLAM8_SETUP_STALE", "BLAM8_SETTINGS_STALE")):
            raise setup_cards.Stale("The card or saved settings changed. Reload before trying again.") from None
        raise
    if response.data != card:
        raise RuntimeError("The card save could not be confirmed. Reload to check its status.")
    return card
