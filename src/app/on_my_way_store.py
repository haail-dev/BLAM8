"""Server-only, tenant-scoped durable On-my-way claims and receipts."""
from app import db


def action(tenant_id, booking_id, key, attempt, operation, data=None, sheet_id=None):
    return db.db().rpc("on_my_way_action", {
        "p_tenant": tenant_id, "p_booking": booking_id, "p_key": key,
        "p_attempt": attempt, "p_operation": operation, "p_data": data or {},
        "p_sheet": sheet_id,
    }).execute().data


def latest(tenant_id, booking_id):
    rows = (db.db().table("on_my_way_actions").select("*")
            .eq("tenant_id", tenant_id).eq("booking_id", booking_id)
            .order("created_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None
