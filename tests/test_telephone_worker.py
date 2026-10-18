"""Bounded offline worker scenarios; no model, carrier or wall-clock waits."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from app import telephone_live as live
from app.telephone_store import StoreError
from app.telephone_provider import AmbiguousSubmission, Submission
from xml.etree.ElementTree import fromstring


def item(name="save_message", status="completed", tool_id="tool-1", args=None):
    return {"type": "response.output_item.done", "item": {"type": "function_call", "status": status,
        "call_id": tool_id, "name": name, "arguments": json.dumps(args if args is not None else
            {"text": "Please call back", "expected_revision": 0, "confirmed": True})}}


class Harness:
    def __init__(self, events=(), *, maximum=60, crm_failure=False, hangup_failure=False):
        self.clock = 0
        self.events = list(events)
        self.sent = []
        self.commands = []
        self.receipts = {}
        self.saved = 0
        self.delivered = set()
        self.crm_failure = crm_failure
        self.hangup_failure = hangup_failure
        self.hangups = 0
        self.cancels = 0
        self.finished = []
        self.polls = 0
        self.event_step = 0
        self.row = {"id": "call-1", "tenant_id": "tenant-1", "provider_call_id": "carrier-1",
                    "status": "answered", "snapshot": {"max_duration_seconds": maximum}}
        self.objectives = SimpleNamespace(command=lambda *a: {"bound": True},
            finish=lambda tenant, call, reason: self.finished.append(reason))

    def command(self, tenant, user, action, payload):
        self.commands.append((action, payload))
        if action == "resolve_model":
            return self.row
        if action == "tool":
            key = payload["tool_call_id"]
            if key in self.receipts:
                return {"result": self.receipts[key], "replayed": True}
            self.saved += 1
            self.receipts[key] = {"confirmed": True, "revision": 1}
            return {"result": self.receipts[key], "replayed": False}
        if action == "outbox_deliver":
            if self.crm_failure:
                raise StoreError("store_unavailable")
            self.delivered.add(payload["call_id"])
            return {"delivered": True}
        if action == "model_usage":
            return {"recorded": True}
        if action == "opt_out":
            self.row["cancel_requested"] = True
            return self.row
        raise AssertionError(action)

    async def recv(self):
        if self.events:
            self.clock += self.event_step
            return json.dumps(self.events.pop(0))
        raise asyncio.TimeoutError()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    @asynccontextmanager
    async def sideband(self, call_id):
        assert call_id == "rtc_call"
        yield self

    def hangup(self, call_id):
        self.hangups += 1
        if self.hangup_failure:
            raise RuntimeError("must-not-log-sensitive-provider-prose")

    def cancel(self, call_id):
        self.cancels += 1
        return True

    def realtime_factory(self):
        return self

    def carrier_factory(self, row):
        return self

    async def wait_for(self, awaitable, timeout):
        try:
            return await awaitable
        except asyncio.TimeoutError:
            self.polls += 1
            self.clock += timeout
            raise

    def run(self, monkeypatch):
        monkeypatch.setattr(live, "time", SimpleNamespace(monotonic=lambda: self.clock))
        monkeypatch.setattr(live.asyncio, "wait_for", self.wait_for)
        asyncio.run(live.run_call(self, self.row, "rtc_call"))

    def outputs(self):
        return [json.loads(event["item"]["output"]) for event in self.sent
                if event["type"] == "conversation.item.create"]


def test_interrupted_or_incomplete_function_items_cannot_mutate(monkeypatch):
    arguments_done = {"type": "response.function_call_arguments.done", "name": "save_message",
        "call_id": "tool-1", "arguments": json.dumps({"text": "Unconfirmed"})}
    harness = Harness([arguments_done, item(status="incomplete"), item(status="cancelled")])
    harness.run(monkeypatch)
    assert harness.saved == 0 and not harness.delivered
    assert not any(action == "tool" for action, _ in harness.commands)


def test_completed_tool_duplicate_reuses_durable_receipt_and_one_business_record(monkeypatch):
    harness = Harness([item(), item()])
    harness.run(monkeypatch)
    assert harness.saved == 1 and harness.delivered == {"call-1"}
    assert len(harness.outputs()) == 2
    assert all(output["delivered"] is True for output in harness.outputs())


def test_carrier_cleanup_runs_even_when_model_hangup_fails(monkeypatch):
    harness = Harness(hangup_failure=True)
    harness.run(monkeypatch)
    assert harness.hangups == 1 and harness.cancels == 1


@pytest.mark.parametrize("maximum,expected,polls", [(60, 30, 6), (12, 12, 3), (1000, 30, 6)])
def test_poll_timeout_respects_silence_and_owner_duration_without_real_sleep(monkeypatch, maximum, expected, polls):
    harness = Harness(maximum=maximum)
    harness.run(monkeypatch)
    assert harness.clock == expected and harness.polls == polls
    assert harness.finished == ["timeout"]
    assert harness.cancels == 1


def test_unsupported_tool_never_becomes_owner_action(monkeypatch):
    harness = Harness([item(name="send_payment", args={"amount": 200})])
    harness.run(monkeypatch)
    assert harness.saved == 0
    assert harness.outputs() == [{"error": "unsupported"}]


def test_continuous_speech_cannot_extend_owner_duration_or_six_hundred_second_ceiling(monkeypatch):
    harness = Harness([{"type": "input_audio_buffer.speech_started"}] * 80, maximum=1000)
    harness.event_step = 10
    harness.run(monkeypatch)
    assert harness.clock == 600
    assert len(harness.events) == 20
    assert harness.finished == ["timeout"] and harness.cancels == 1


def test_remaining_duration_overrides_original_snapshot_limit(monkeypatch):
    harness = Harness(maximum=300)
    harness.row["remaining_seconds"] = 7
    harness.run(monkeypatch)
    assert harness.clock == 7 and harness.polls == 2


def test_crm_failure_preserves_message_and_reports_pending_delivery(monkeypatch):
    harness = Harness([item()], crm_failure=True)
    harness.run(monkeypatch)
    assert harness.saved == 1 and not harness.delivered
    assert harness.outputs()[0]["delivered"] is False
    assert harness.outputs()[0]["delivery"] == "pending_owner_attention"


def test_usage_receipt_drops_untrusted_prose_and_only_keeps_numeric_counters(monkeypatch):
    harness = Harness([{"type": "response.done", "response": {"id": "resp-1", "usage": {
        "total_tokens": 12, "input_tokens": 10, "output_tokens": 2,
        "transcript": "private", "input_token_details": {"audio_tokens": 8, "text_tokens": 2,
            "cached_tokens": 0, "transcript": "private"}}}}])
    harness.run(monkeypatch)
    usages = [payload["usage"] for action, payload in harness.commands if action == "model_usage"]
    assert usages == [{"total_tokens": 12, "input_tokens": 10, "output_tokens": 2,
                      "input_token_details": {"audio_tokens": 8, "text_tokens": 2, "cached_tokens": 0}}]


def test_usage_receipt_failure_does_not_drop_next_caller_message(monkeypatch):
    harness = Harness([{"type": "response.done", "response": {"id": "resp-1", "usage": {
        "total_tokens": 12}}}, item()])
    original = harness.command
    def command(tenant, user, action, payload):
        if action == "model_usage":
            raise StoreError("store_unavailable")
        return original(tenant, user, action, payload)
    harness.command = command
    harness.run(monkeypatch)
    assert harness.saved == 1 and harness.delivered == {"call-1"}


def service_row():
    return {"id": "call-1", "tenant_id": "tenant-1", "provider_call_id": "carrier-1",
        "status": "answered", "remaining_seconds": 40, "snapshot": {"provider": "twilio",
            "from_number": "+442079460100", "to_number": "+442079460101", "objective": "Requested callback",
            "max_duration_seconds": 90}}


def test_service_voice_retries_reuse_binding_and_enforce_total_call_duration(monkeypatch):
    row = service_row()
    tokens, limits = [], []
    def command(tenant, user, action, payload):
        if action == "admit":
            return row
        assert action == "bind_sip"
        tokens.append(payload["token"])
        return row
    monkeypatch.setattr(live.settings, "telephone_openai_webhook_secret", "offline-binding-secret")
    monkeypatch.setattr(live.settings, "telephone_openai_project_id", "proj_example")
    monkeypatch.setattr(live.settings, "telephone_webhook_base_url", "https://telephone.example")
    adapter = SimpleNamespace(limit_duration=lambda call, total: limits.append((call, total)))
    service = live.TelephoneLiveService(SimpleNamespace(command=command), objectives=SimpleNamespace(),
                                         carrier_factory=lambda row: adapter)
    _, first = service.voice({"CallSid": "carrier-1", "AccountSid": "account-1"}, {})
    _, second = service.voice({"CallSid": "carrier-1", "AccountSid": "account-1"}, {})
    assert first == second and tokens[0] == tokens[1] and len(tokens[0]) == 64
    assert limits == [("carrier-1", 90), ("carrier-1", 90)]
    assert fromstring(first).find("Dial").attrib["timeLimit"] == "40"
    row["session"] = {"consumed": True}
    assert service.voice({"CallSid": "carrier-1"}, {})[1] == "<Response/>"
    assert len(tokens) == 2  # Never generate another SIP leg after consumption.


def test_service_ambiguous_submission_preserves_attempt_and_never_autogenerates_events():
    row = service_row()
    submissions = []
    def command(tenant, user, action, payload):
        if action == "read":
            return row
        if action == "claim_outbound":
            if row.get("attempt_id"):
                raise StoreError("already_dispatched")
            row["attempt_id"] = payload["attempt_id"]
            row["status"] = "submitting"
            return row
        if action == "unknown_outbound":
            assert payload["attempt_id"] == row["attempt_id"]
            row["status"] = "unknown"
            return row
        raise AssertionError(action)
    def submit(spec):
        submissions.append(spec)
        raise AmbiguousSubmission("submission_unknown")
    adapter = SimpleNamespace(verify_caller_id=lambda number: True, submit=submit,
        events=lambda *args: pytest.fail("Real transport must not invent events"))
    service = live.TelephoneLiveService(SimpleNamespace(command=command),
        objectives=SimpleNamespace(command=lambda *args: {"bound": True}), carrier_factory=lambda row: adapter)
    assert service.dispatch("tenant-1", "owner-1", "call-1")["status"] == "unknown"
    with pytest.raises(StoreError, match="already_dispatched"):
        service.dispatch("tenant-1", "owner-1", "call-1")
    assert len(submissions) == 1


def test_service_cancellation_during_submission_is_forwarded_after_provider_id_arrives():
    row = service_row()
    cancelled = []
    def command(tenant, user, action, payload):
        if action == "claim_outbound":
            row["attempt_id"] = payload["attempt_id"]
        elif action == "submitted_outbound":
            row["provider_call_id"] = payload["provider_call_id"]
        elif action != "read":
            raise AssertionError(action)
        return row
    def submit(spec):
        row["cancel_requested"] = True
        return Submission("carrier-new", simulated=False)
    adapter = SimpleNamespace(verify_caller_id=lambda number: True, submit=submit,
                              cancel=lambda call_id: cancelled.append(call_id))
    service = live.TelephoneLiveService(SimpleNamespace(command=command),
        objectives=SimpleNamespace(command=lambda *args: {"bound": True}), carrier_factory=lambda row: adapter)
    result = service.dispatch("tenant-1", "owner-1", "call-1")
    assert cancelled == ["carrier-new"] and result["cancel_requested"] is True
    assert result["status"] != "cancelled"  # Acknowledgment is not terminal proof.


def test_transfer_uses_remaining_duration_and_never_claims_human_answer(monkeypatch):
    row = service_row()
    row["remaining_seconds"] = 12
    row["transfer"] = {"id": "transfer-1", "status": "requested", "target": "+442079460102", "timeout_seconds": 20}
    requests = []
    adapter = SimpleNamespace(transfer=lambda *args, **kwargs: requests.append((args, kwargs)))
    service = live.TelephoneLiveService(SimpleNamespace(command=lambda *args: row), objectives=SimpleNamespace(),
                                         carrier_factory=lambda row: adapter)
    monkeypatch.setattr(live.settings, "telephone_webhook_base_url", "https://telephone.example")
    result = service.transfer(row)
    assert requests[0][0][3] <= row["remaining_seconds"]
    assert requests[0][1]["time_limit"] == 12
    assert result["status"] == "accepted" and result["human_answer_verified"] is False
    row["remaining_seconds"] = 2
    with pytest.raises(StoreError):
        service.transfer(row)
    assert len(requests) == 1


def test_coarse_transfer_completion_does_not_poison_child_sequence_or_verify_human():
    row = service_row()
    row["transfer"] = {"id": "transfer-1", "status": "requested", "target": "+442079460102"}
    updates = []
    def command(tenant, user, action, payload):
        if action == "resolve":
            return row
        assert action == "transfer_update"
        updates.append(payload)
        return row
    service = live.TelephoneLiveService(SimpleNamespace(command=command), objectives=SimpleNamespace())
    query = {"transfer_id": "transfer-1"}
    coarse = {"AccountSid": "account-1", "CallSid": "carrier-1", "DialCallSid": "child-1", "DialCallStatus": "completed"}
    assert service.transfer_event(coarse, query, ended=True) is row
    assert updates == []
    child = {"AccountSid": "account-1", "ParentCallSid": "carrier-1", "CallSid": "child-1",
             "CallStatus": "in-progress", "SequenceNumber": "2", "To": "+442079460102"}
    service.transfer_event(child, query)
    assert updates[0]["sequence"] == 2
    assert updates[0]["provider_leg_id"] == "child-1"
    assert updates[0]["parent_call_id"] == "carrier-1"
    assert updates[0]["target_number"] == "+442079460102"
    assert updates[0]["status"] == "answered"  # Raw carrier evidence; SQL retains accepted until operator proof.
    assert row["transfer"]["status"] == "requested"  # Service cannot manufacture operator confirmation.
