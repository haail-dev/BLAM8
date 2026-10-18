"""Explicit appointment capacity, with no inferred count or reuse of stay space."""
from copy import deepcopy

from app import services, setup_rows
from app.setup_cards import Invalid, PENDING, _named


def _find(rows, name):
    matches = [r for r in rows or [] if services._norm(r.get("name")) == services._norm(name)]
    if len(matches) > 1:
        raise Invalid("Saved rows have duplicate names. Review them on the diary page.")
    return matches[0] if matches else None


def _stay_names(cfg):
    appointments = {services._norm(s.get("resource") or s["name"]) for s in cfg.get("service_types") or []}
    return {services._norm(r["name"]) for r in cfg.get("resource_types") or []} - appointments


def bind(card, selected, before, result, changes):
    selected_services = {services._norm(l["fields"]["name"]): l for l in selected if l["name"] == "service"}
    selected_resources = {services._norm(l["fields"]["name"]): l for l in selected if l["name"] == "resource"}
    pools = result.setdefault("resource_types", [])
    if pools is None:
        pools = result["resource_types"] = []
    # A new pool named on a pending appointment must not become a stay space
    # just because only its resource line was selected.
    for line in card["lines"]:
        if line["status"] not in PENDING or line["name"] != "service":
            continue
        label = line["fields"].get("name")
        key = services._norm(label)
        if key in selected_resources and key not in selected_services and not _find(before.get("service_types"), label):
            raise Invalid("Accept the appointment and its capacity together.")
    for svc in result.get("service_types") or []:
        key = services._norm(svc["name"])
        line = selected_services.get(key)
        old_svc = _find(before.get("service_types"), svc["name"])
        old_pool = _find(before.get("resource_types"), svc["name"])
        if old_svc and old_svc.get("resource") and services._norm(old_svc["resource"]) != key:
            raise Invalid("Review the existing appointment's shared capacity link on the diary page; this card cannot move its bookings.")
        if services.timing(svc) != "time":
            raise Invalid("Appointments on a stay diary need exact-time timing. Review this appointment's timing.")
        if svc.get("resource") and services._norm(svc["resource"]) != key:
            raise Invalid("A stay-diary appointment needs its own same-name capacity pool, separate from stay spaces.")
        if old_pool and old_svc is None:
            raise Invalid("That name already identifies a stay space. Choose a different appointment name.")
        pool = _find(pools, svc["name"])
        if line:
            supplied = line["fields"]
            count = supplied.get("at_once")
            # setup_rows has validated native/legacy number forms; retain the
            # parsed integer rather than interpreting unchecked model values.
            if "at_once" in supplied:
                count = setup_rows.parse("diary_settings", "service", _named("service", svc) | {"at_once": count})["fields"]["at_once"]
            resource_line = selected_resources.get(key)
            if count is not None and resource_line and "count" in resource_line["fields"] and pool["count"] != count:
                raise Invalid("The appointment and resource propose different capacities. Keep one agreed count.")
            if pool is None:
                if count is None:
                    raise Invalid("Choose how many appointments can run at once, or accept its same-name resource with an explicit capacity.")
                pool = {"name": svc["name"], "count": count}
                pools.append(pool)
            elif count is not None:
                pool["count"] = count
            svc["resource"] = pool["name"]
        elif pool is None or not svc.get("resource"):
            raise Invalid("Review the existing appointment's missing capacity link before accepting these rows.")
    if before.get("capacity_per_window") is not None and not _stay_names(before) and _stay_names(result):
        raise Invalid("Changing shared stay capacity into named spaces needs a review of existing bookings on the diary page first.")
    for change in changes:
        name = change["name"]
        rows = result.get("service_types") if name == "service" else pools
        after = _find(rows, change["after"]["name"])
        change["after"] = deepcopy(after)
        fields = _named(name, after)
        prior = _named(name, change["before"]) if change["before"] else {}
        if name == "service":
            pool = _find(pools, after["resource"])
            old = _find(before.get("resource_types"), (change["before"] or {}).get("resource") or after["name"])
            fields["at_once"] = pool["count"]
            if old:
                prior["at_once"] = old["count"]
            change.update(resource_before=deepcopy(old), resource_after=deepcopy(pool),
                          notes=["Appointment capacity is separate from stay spaces."])
        else:
            appointment = _find(result.get("service_types"), after["name"])
            change["notes"] = (["Capacity for " + appointment["name"] + " appointments, separate from stay spaces."]
                               if appointment else ["Capacity for stay spaces."])
        parsed = setup_rows.parse("diary_settings", name, fields)
        if parsed["problems"]:
            raise Invalid("; ".join(parsed["problems"]))
        fields = parsed["fields"]
        if prior:
            prior = setup_rows.parse("diary_settings", name, prior)["fields"] or prior
        change.update(fields=fields, review_before=prior, review_after=deepcopy(fields))
