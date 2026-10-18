"""Role checks on the ACTIONS, not just the pages (security review 2026-09,
H1/H2: "roles are enforced only where a handler opts in — refunds, Stripe
connect, mailbox replace and closing the account were open to every member").

People (src/app/people.py) decides what each role may do; these helpers put that
decision in front of the routes that change money, connections or the account
itself. They read the membership FRESH (no navcache) so a demotion or a switch-
off bites at once, and a switched-off member is refused everything here.

No membership row = the account's own owner (every tenant that predates
People, and a business mid-onboarding) — the same rule people.can_open uses.
"""
from __future__ import annotations

from fastapi import HTTPException

from app import db, people

_NO = "You don't have permission to do that. Ask whoever runs this account."


def member_of(ctx) -> dict | None:
    """The acting person's membership, read fresh. While the owner is viewing
    the app as someone, ctx.user_id is that person — so the check answers as
    them (the owner sees exactly what their person could do)."""
    if not getattr(ctx, "user_id", None):
        return None
    row = db.get_member(ctx.tenant_id, ctx.user_id)
    return people.member(row) if row else None


def _refuse(detail: str = _NO):
    raise HTTPException(status_code=403, detail=detail)


def require_owner(ctx) -> None:
    """Billing, payouts and closing the account: the real owner only, and never
    while viewing the app as someone else."""
    if getattr(ctx, "viewing_as", None):
        _refuse("Stop viewing as someone else first.")
    m = member_of(ctx)
    if m is None:
        return
    if not m.get("active", True) or people.normalise_role(m.get("role")) != "owner":
        _refuse("Only the owner can do that.")


def require_admin(ctx) -> None:
    """Connections and account-wide settings: owner or manager, switched on."""
    m = member_of(ctx)
    if m is None:
        return
    if not m.get("active", True) or not people.is_admin(m):
        _refuse()


def require_ability(ctx, page: str, ability: str) -> None:
    """One ticked ability behind a door (owner/manager always; accountant never)."""
    m = member_of(ctx)
    if m is not None and not m.get("active", True):
        _refuse()
    if not people.can_do(m, page, ability):
        _refuse()


def sees_business_wide(ctx) -> bool:
    """Owner/manager (switched on): the business-wide Concierge turns — the
    morning brief, payment and quote notes — are theirs to see (H6)."""
    try:
        m = member_of(ctx)
    except Exception:  # noqa: BLE001 — fail closed: their own turns only
        return False
    return m is None or bool(m.get("active", True) and people.is_admin(m))
