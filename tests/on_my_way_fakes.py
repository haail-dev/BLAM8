"""I/O-only On-my-way doubles; real SQL transitions have separate database proof."""
from copy import deepcopy
import json
from threading import Lock
from uuid import uuid4

from app import on_my_way


def claims(monkeypatch):
    rows = {}
    lock = Lock()

    def action(tenant, booking, key, attempt, operation, data=None, sheet_id=None):
        data = data or {}
        with lock:
            index = tenant, booking, key
            row = rows.get(index)
            if operation == "owner_resend":
                if sheet_id or not row or row["id"] != latest(tenant, booking)["id"]:
                    return {"error": "stale_action"}
                if row["state"] != "unknown" or row["attempt"] != data.get("resend_attempt"):
                    return {**deepcopy(row), "owned": False}
                row["receipt"]["reconciliation_evidence"] = json.dumps({
                    "reason": "owner chose to resend", "initiator": data.get("initiator"),
                    "previous_attempt": row["attempt"], "eta": data["eta"]})
                row.setdefault("history", []).append(deepcopy({k:v for k,v in row.items() if k != "history"}))
                row.update(state="pre_send_failure", eta=data["eta"],
                           person_name=data.get("person_name"), initiator=data.get("initiator"), receipt={})
                operation = "claim"
            if operation == "claim":
                if row and row["state"] != "pre_send_failure":
                    return {**deepcopy(row), "owned": False}
                if not row:
                    if key != "initial":
                        previous = latest(tenant, booking)
                        if not previous or previous["state"] != "sent" or key != "after:" + previous["id"]:
                            return {"error": "stale_action"}
                    row = rows[index] = {"id": str(uuid4()), "action_key": key,
                                         "receipt": {}, **data}
                row.update(state="preparing", attempt=attempt)
            elif operation == "notice":
                if row.get("notice_state") or row["state"] not in ("unknown", "sent", "pre_send_failure"):
                    return {"owned": False}
                row["notice_state"] = "unknown"
                row["notice_id"] = str(uuid4())
                row["notice_body"] = (data.get("what", "") + " · " + (
                    "the customer's been told" if row["state"] == "sent" else
                    "customer delivery is unknown" if row["state"] == "unknown" else
                    "customer message was not sent"))
            elif operation == "notice_attempted":
                row["notice_state"] = "attempted"
            elif row["attempt"] != attempt:
                return {"error": "stale_attempt"}
            elif operation == "dispatch":
                row.update(state="unknown", receipt=deepcopy(data))
            elif operation == "sent":
                row["state"] = "sent"
                row["receipt"].update(data)
            elif operation in ("fail", "not_sent"):
                row.update(state="pre_send_failure", receipt=deepcopy(data))
            return {**deepcopy(row), "owned": True}

    def latest(tenant, booking):
        matches = [v for (t, b, k), v in rows.items() if (t, b) == (tenant, booking)]
        return deepcopy(matches[-1]) if matches else None

    monkeypatch.setattr(on_my_way.store, "action", action)
    monkeypatch.setattr(on_my_way.store, "latest", latest)
    return rows
