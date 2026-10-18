"""The four reviewed booking settings. No business values are invented here."""
from datetime import datetime, time, timezone
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Generic, Literal, TypeVar
from zoneinfo import ZoneInfo

FIELDS = {
    "capacity": ("capacity_per_window",),
    "working_days": ("working_days",),
    "opening_hours": ("open_time", "close_time"),
    "timezone": ("timezone",),
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENGINE_TIMEZONE = ZoneInfo("Europe/London")
COMPILER_VERSION = "booking-profile-1"
Value = TypeVar("Value")


@dataclass(frozen=True)
class Setting(Generic[Value]):
    state: str
    values: tuple[Value, ...]
    valid: bool
    review_status: str | None


@dataclass(frozen=True)
class ProfileIdentity:
    tenant_id: str | None
    mailbox_id: str | None
    compiler: str
    booking_revision: int | None
    source_revision: str | None


@dataclass(frozen=True)
class BookingProfile:
    identity: ProfileIdentity
    read_state: Literal["read", "failed"]
    capacity: Setting[int]
    working_days: Setting[str]
    opening_hours: Setting[str]
    timezone: Setting[str]
    unresolved_settings: tuple[str, ...]
    stay: bool
    services: bool
    appointments: bool

    @property
    def ready(self) -> bool:
        return self.read_state == "read" and not self.unresolved_settings

    @property
    def message(self) -> str:
        if self.read_state == "failed":
            return "Could not verify booking settings. Hold for owner review."
        return ("Booking is unavailable. Hand this request to the owner. "
                "Do not promise a booking, say full or closed, or ask the customer to choose business settings. "
                "Unresolved settings: " + ", ".join(self.unresolved_settings)) if self.unresolved_settings else ""

    @property
    def prompt_sections(self) -> tuple[tuple[str, str], ...]:
        return (("booking_readiness", self.message),) if self.message else ()

    def as_policy(self) -> dict:
        """Existing wire format; generated text is never an authority input."""
        result = {"ready": self.ready, "unresolved_settings": list(self.unresolved_settings),
                  "states": {key: getattr(self, key).state for key in FIELDS},
                  "message": self.message, "revision": self.identity.booking_revision}
        if self.read_state == "failed":
            result["read_state"] = "failed"
        return result


def business_timezone(cfg):
    value = (cfg or {}).get("timezone")
    if value != ENGINE_TIMEZONE.key:
        raise ValueError("Choose Europe/London. Other timezones are not supported yet.")
    return ENGINE_TIMEZONE


def validate(setting, values):
    if setting == "capacity":
        value = values[0]
        if isinstance(value, bool) or str(value).strip() != str(int(value)) or not 0 <= int(value) <= 9999:
            raise ValueError("Capacity must be a whole number from 0 to 9999.")
        return (int(value),)
    if setting == "working_days":
        days = str(values[0]).replace(" ", "").lower().split(",") if values[0] else []
        if any(d not in DAYS for d in days):
            raise ValueError("Choose valid working days.")
        return (",".join(d for d in DAYS if d in days),)
    if setting == "opening_hours":
        try:
            opening, closing = (time.fromisoformat(str(v)) for v in values)
        except (TypeError, ValueError):
            raise ValueError("Enter valid opening and closing times.") from None
        if opening >= closing:
            raise ValueError("Closing time must be after opening time.")
        return (opening.isoformat(), closing.isoformat())
    business_timezone({"timezone": values[0]})
    return tuple(values)


def compile_profile(cfg, *, tenant_id=None, mailbox_id=None, fleet=False,
                    read_failed=False) -> BookingProfile:
    """One interpreter for readiness, typed values and existing booking shapes.

    The content revision also covers settings writers which do not increment the
    four-setting revision. It is comparable across workers, not a sequence number.
    """
    from app import intake, services
    cfg = cfg or {}
    if tenant_id is not None and cfg.get("tenant_id") not in (None, tenant_id):
        read_failed = True
    if read_failed:
        cfg = {}
    metadata = cfg.get("booking_setting_review") or {}
    unresolved, settings = [], {}
    for setting, keys in FIELDS.items():
        values = tuple(cfg.get(k) for k in keys)
        state = metadata.get(setting, {}).get("state", "set" if all(v is not None for v in values) else "unset")
        checked, valid = (), False
        pools = cfg.get("resource_types") or []
        valid_pools = bool(pools) and all(isinstance(r, dict) and r.get("name")
                                          and isinstance(r.get("count"), int) and r["count"] > 0 for r in pools)
        if setting == "capacity" and state == "not_applicable" and (fleet or valid_pools):
            valid = True
        elif state == "set" and all(v is not None for v in values):
            try:
                checked = validate(setting, values)
                valid = not ((setting == "capacity" and checked[0] == 0)
                             or (setting == "working_days" and not checked[0]))
            except (ValueError, TypeError, OverflowError):
                pass
        if not valid:
            unresolved.append(setting)
        settings[setting] = Setting(state, checked, valid, metadata.get(setting, {}).get("review_status"))
    identity = ProfileIdentity(tenant_id, mailbox_id, COMPILER_VERSION,
        None if read_failed else cfg.get("booking_revision", 0),
        None if read_failed else sha256(json.dumps([cfg, fleet], sort_keys=True,
            separators=(",", ":"), default=str).encode()).hexdigest())
    stay = intake.stay_mode(cfg)
    has_services = services.configured(cfg.get("service_types") or [])
    return BookingProfile(identity, "failed" if read_failed else "read", **settings,
        unresolved_settings=tuple(unresolved), stay=stay,
        services=not stay and has_services, appointments=stay and has_services)


def compile_policy(cfg, *, fleet=False):
    return compile_profile(cfg, fleet=fleet).as_policy()


def refusal(policy):
    if isinstance(policy, BookingProfile):
        policy = policy.as_policy()
    if policy.get("read_state") == "failed":
        return {"error": "could_not_verify", "outcome": "could_not_verify",
                "held": False, "owner_review": True, "message": policy["message"]}
    if policy["ready"]:
        return None
    return {"error": "unsupported", "outcome": "unsupported", "held": False,
            "handoff": "owner", "unresolved_settings": policy["unresolved_settings"],
            "message": policy["message"]}


def apply_settings(existing, submitted, states, confirmed, actor, revision):
    """Omitted keys are untouched; blank values clear; zero remains an explicit value.

    Review requires an explicit checkbox or a changed value, never a page view/save.
    The database compares the revision again while holding the row lock.
    """
    existing = existing or {}
    if int(revision) != int(existing.get("booking_revision", 0)):
        raise ValueError("These settings changed. Reload before applying your changes.")
    metadata = {k: dict(v) for k, v in (existing.get("booking_setting_review") or {}).items()}
    changes = {}
    next_revision = int(revision) + 1
    for setting, keys in FIELDS.items():
        if not any(k in submitted for k in keys) and setting not in states and setting not in confirmed:
            continue
        values = tuple(submitted.get(k, existing.get(k)) for k in keys)
        state = states.get(setting) or (
            metadata.get(setting, {}).get("state") if not any(k in submitted for k in keys) else None
        ) or ("unset" if any(v is None or v == "" for v in values) else "set")
        if state == "set" and setting != "working_days" and any(v is None or v == "" for v in values):
            state = "unset"
        # An explicit empty selection means no working days, not an omitted field.
        if setting == "working_days" and values == ("",) and states.get(setting) == "set":
            state = "off"
        if state not in ("unset", "not_applicable", "off", "set"):
            raise ValueError("Unknown setting state.")
        if state == "not_applicable" and setting != "capacity":
            raise ValueError("This setting is required for booking and cannot be not applicable.")
        if state == "set":
            values = validate(setting, values)
        else:
            values = tuple(None for _ in keys)
        old_values = tuple(existing.get(k) for k in keys)
        old_state = metadata.get(setting, {}).get("state", "set" if all(v is not None for v in old_values) else "unset")
        # Time columns arrive with seconds; compare their validated form.
        try:
            if all(v is not None for v in old_values):
                old_values = validate(setting, old_values)
        except (ValueError, TypeError, OverflowError):
            pass
        changed = values != old_values or state != old_state
        if not changed and setting not in confirmed:
            continue
        changes.update(zip(keys, values))
        metadata[setting] = {**metadata.get(setting, {}),
                             "carried_over_source": metadata.get(setting, {}).get("carried_over_source") or metadata.get(setting, {}).get("source"),
                             "state": state, "review_status": "confirmed",
                             "source": "owner", "revision": next_revision,
                             "confirmed_by": actor,
                             "confirmed_at": datetime.now(timezone.utc).isoformat()}
    if changes:
        changes.update(booking_setting_review=metadata, booking_revision=next_revision)
    return changes
