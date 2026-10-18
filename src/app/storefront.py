"""Storefront OS — the drafter's stock brain (docs/os/storefront.md §3, §6).

The Storefront twin of src/app/scheduling.py's agent tooling. Two channel-agnostic tools,
defined and CONDITIONALLY INJECTED only when the tenant's `storefront` module is active —
exactly how SCHEDULING_TOOLS/FLEET_TOOLS are injected in the diary. Windows/diary tenants
never see them; a non-storefront tenant's drafting is byte-identical (no tools, no context).

  check_stock(product_name, qty?)      — reads live availability off shop_products
                                         (available = qty_on_hand - qty_reserved) + the price.
  reserve_stock(product_name, qty, …)  — the "hold before you reply" mandate: performs the
                                         ATOMIC reservation (db.shop_hold_stock_if_available,
                                         the same RPC the counter uses) as part of executing
                                         the tool, so the drafted reply only ever confirms
                                         stock that is genuinely held. Out-of-stock (RPC
                                         returns None) comes back as a clean no-hold result so
                                         the drafter offers an alternative / a wait — it never
                                         invents a hold.

The capacity decision itself lives in the DB (the RPC row-locks the product), never in
Python — like the diary, this layer can only ever be too cautious, never wrong.
"""
from datetime import date, datetime

from app import db
from app.scheduling import DIARY_TZ

# Static module constants: the tool list must be byte-stable across requests or it
# invalidates the prompt cache (tools render first in the prompt), the same reason the
# scheduling tools are frozen constants. Nothing volatile lives in the schema.
STOREFRONT_TOOLS = [
    {
        "name": "check_stock",
        "description": (
            "Check the shop's live stock for a product the customer asked about. Call this "
            "BEFORE telling a customer whether something is in, out, or how many are left — "
            "never rely on memory or guesswork for availability or price. Returns the "
            "product's current availability (how many can be sold right now — on-hand minus "
            "what's already reserved) and its unit price. If the named product isn't in the "
            "shop's catalogue at all, `found` is false — say a colleague will check rather "
            "than inventing a price or a quantity."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string",
                                 "description": "The product as the customer named it, e.g. "
                                                "'sourdough' or 'Maris Piper potatoes'."},
                "qty": {"type": "number",
                        "description": "How many the customer wants — ONLY when they said a "
                                       "number. The result tells you if that many are available."},
            },
            "required": ["product_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reserve_stock",
        "description": (
            "Hold stock for this customer's collection. You MUST call this whenever the "
            "customer asks you to keep, save, hold or reserve a product AND check_stock "
            "showed enough is available — do not merely tell them it's available, and never "
            "say anything is held, saved or reserved unless this tool returned held:true. "
            "The reservation is REAL — it takes the stock off the shelf immediately — so once "
            "it returns held:true you may truthfully confirm it. If it returns held:false the "
            "stock isn't there: offer an alternative or ask them to wait for the next "
            "delivery, and NEVER claim a hold. At most one reservation per email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string",
                                 "description": "The product to reserve, as the customer "
                                                "named it."},
                "qty": {"type": "number",
                        "description": "How many to hold for the customer."},
                "ready_date": {"type": "string", "format": "date",
                               "description": "The day the customer will collect, YYYY-MM-DD "
                                              "— ONLY when they said one."},
            },
            "required": ["product_name", "qty"],
            "additionalProperties": False,
        },
    },
]


def storefront_diary_line(products: list[dict] | None = None) -> str:
    """One honest sentence describing the shop for the prompt's <storefront> section —
    the shop answers off live stock, and a reservation really holds it."""
    return ("You also answer this shop's inbox off its LIVE stock. Use check_stock before "
            "telling a customer what's in or its price, and reserve_stock to actually hold "
            "something they want to collect — a reservation takes it off the shelf.")


def _available(prod: dict) -> float:
    """Sellable-now quantity: on-hand minus what's already reserved (never below 0)."""
    on_hand = float(prod.get("qty_on_hand") or 0)
    reserved = float(prod.get("qty_reserved") or 0)
    return max(0.0, on_hand - reserved)


def make_storefront_executor(tenant_id: str, contact_id: str | None = None,
                             thread_id: str | None = None, channel: str = "email",
                             now: datetime | None = None):
    """The Storefront tool executor for one email: (name, args) -> JSON-able dict.

    Returns (execute, state). state['reservations'] collects the transactions actually
    reserved this email, so the pipeline can link them to the draft row / release on a
    duplicate. The one-reservation-per-email rule is enforced HERE in code (the prompt
    merely explains it), exactly like the diary's one-hold-per-email.

    The "hold before you reply" mandate: reserve_stock performs the ATOMIC reservation
    (db.shop_hold_stock_if_available — the P1 RPC) as part of executing the tool, so the
    draft only ever confirms stock that is genuinely held. Out-of-stock (RPC None) returns
    held:false — a clean signal, never a fabricated hold."""
    state: dict = {"reservations": []}

    def _find(name: str) -> dict | None:
        """The catalogue row for a customer-named product (dedupes on lower(name), like
        the counter). Best-match on an exact (case-insensitive) name; else a single
        substring hit so 'sourdough' finds 'Sourdough loaf'."""
        want = (name or "").strip()
        if not want:
            return None
        prod = db.shop_get_product_by_name(tenant_id, want)
        if prod:
            return prod
        matches = db.shop_list_products(tenant_id, q=want, limit=5)
        return matches[0] if len(matches) == 1 else None

    def _check(args: dict) -> dict:
        name = str(args.get("product_name") or "").strip()
        prod = _find(name)
        if not prod:
            return {"found": False, "product_name": name,
                    "message": "That product isn't in the shop's catalogue. Do not quote a "
                               "price or a quantity — say a colleague will check for them."}
        avail = _available(prod)
        out = {"found": True, "product_name": prod["name"], "available": avail,
               "unit": prod.get("unit") or "each"}
        if prod.get("unit_price") is not None:
            out["unit_price"] = float(prod["unit_price"])
        want = args.get("qty")
        if want not in (None, ""):
            try:
                want = float(want)
                out["requested"] = want
                out["enough"] = avail >= want
            except (TypeError, ValueError):
                pass
        if avail <= 0:
            out["message"] = ("None available right now — offer to hold some from the next "
                              "delivery or suggest an alternative; do not promise stock.")
        return out

    def _reserve(args: dict) -> dict:
        name = str(args.get("product_name") or "").strip()
        try:
            qty = float(args.get("qty"))
        except (TypeError, ValueError):
            return {"error": "invalid_qty",
                    "message": "How many should be held? Ask the customer for a quantity."}
        if qty <= 0:
            return {"error": "invalid_qty",
                    "message": "How many should be held? Ask the customer for a quantity."}
        prod = _find(name)
        if not prod:
            return {"held": False, "found": False, "product_name": name,
                    "message": "That product isn't in the shop's catalogue, so nothing can be "
                               "held — say a colleague will check for them."}
        ready = str(args.get("ready_date") or "").strip() or None
        # The ATOMIC reservation — the SAME RPC the counter uses (db.shop_hold_stock_if_available).
        # None = out_of_stock: a clean no-hold result, NEVER a fabricated hold.
        tx = db.shop_hold_stock_if_available(
            tenant_id, prod["id"], qty, contact_id=contact_id, ready_date=ready,
            channel=channel, thread_id=thread_id, source="agent")
        if tx is None:
            avail = _available(prod)
            return {"held": False, "product_name": prod["name"], "available": avail,
                    "message": "Not enough available to hold that many — offer the customer the "
                               f"quantity that IS available ({avail:g}) or the next delivery; "
                               "do NOT say anything was reserved."}
        state["reservations"].append(tx)
        out = {"held": True, "transaction_id": tx.get("id"), "product_name": prod["name"],
               "qty": qty, "status": "reserved"}
        if ready:
            out["ready_date"] = ready
        return out

    def execute(name: str, args: dict) -> dict:
        if name == "check_stock":
            return _check(args)
        if name == "reserve_stock":
            # One reservation per email, enforced HERE in code (the prompt explains it).
            if state["reservations"]:
                held = state["reservations"][0]
                return {"error": "already_reserved", "transaction_id": held.get("id"),
                        "message": "A reservation is already placed for this email — at most one "
                                   "per email. Reference the existing hold in your reply."}
            return _reserve(args)
        return {"error": "unknown_tool", "message": f"No tool named {name}."}

    return execute, state


def _today(now: datetime | None = None) -> date:
    return (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ).date()
