"""Offline telephone adapter boundary. No real provider is enabled here."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re


class DefiniteFailure(RuntimeError):
    """Submission definitely did not place a call."""


class AmbiguousSubmission(RuntimeError):
    """Submission might have placed a call; never retry automatically."""


@dataclass(frozen=True)
class CallSpec:
    call_id: str
    attempt_id: str
    destination: str
    objective: str
    max_duration_seconds: int
    script: str = ""
    scenario: str = "completed"


@dataclass(frozen=True)
class Submission:
    provider_call_id: str
    simulated: bool = True


@dataclass(frozen=True)
class ProviderEvent:
    call_id: str
    provider_call_id: str
    tenant_id: str
    event_id: str
    sequence: int
    status: str
    outcome: str = "none"
    duration_seconds: int = 0
    cost_minor: int = 0
    message_summary: str = ""


STATUSES = frozenset({"initiated", "ringing", "answered", "completed", "failed",
                      "busy", "no_answer", "cancelled"})
OUTCOMES = frozenset({"none", "message_taken", "callback_requested", "declined", "unknown"})
SCENARIOS = frozenset({"completed", "failed", "busy", "no_answer", "timeout", "ambiguous"})


def _integer(value, upper):
    return type(value) is int and 0 <= value <= upper


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value))


def sign_simulation_event(raw_body: bytes, secret: str) -> str:
    if not isinstance(secret, str) or len(secret) < 32:
        raise DefiniteFailure("simulation_secret_invalid")
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_simulation_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not isinstance(signature, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", signature):
        return False
    try:
        expected = sign_simulation_event(raw_body, secret)
    except DefiniteFailure:
        return False
    return hmac.compare_digest(expected, signature.lower())


def parse_event(raw_body: bytes) -> ProviderEvent:
    """Parse only bounded fields; caller must verify signature before dispatch."""
    if not isinstance(raw_body, bytes) or len(raw_body) > 8192:
        raise DefiniteFailure("invalid_event")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    try:
        body = json.loads(raw_body, object_pairs_hook=unique_pairs)
        if not isinstance(body, dict) or set(body) - set(ProviderEvent.__dataclass_fields__):
            raise ValueError("fields")
        event = ProviderEvent(**body)
        if not all(_identifier(getattr(event, key)) for key in
                   ("call_id", "provider_call_id", "tenant_id", "event_id")):
            raise ValueError("identity")
        if not isinstance(event.status, str) or event.status not in STATUSES:
            raise ValueError("status")
        if not isinstance(event.outcome, str) or event.outcome not in OUTCOMES:
            raise ValueError("outcome")
        if not _integer(event.sequence, 1_000_000) or not _integer(event.duration_seconds, 7200):
            raise ValueError("range")
        if not _integer(event.cost_minor, 10_000_000):
            raise ValueError("cost")
        if not isinstance(event.message_summary, str) or len(event.message_summary) > 1000:
            raise ValueError("summary")
        return event
    except (ValueError, TypeError, UnicodeError):
        raise DefiniteFailure("invalid_event") from None


class SimulatedProvider:
    """Explicit synthetic scenarios; never contacts a telephone network."""
    def __init__(self):
        self._submissions = {}

    def submit(self, spec: CallSpec) -> Submission:
        if (not _identifier(spec.call_id) or not _identifier(spec.attempt_id)
                or not isinstance(spec.destination, str)
                or not re.fullmatch(r"\+[1-9][0-9]{7,14}", spec.destination)
                or not isinstance(spec.objective, str) or not 1 <= len(spec.objective) <= 4000
                or not isinstance(spec.script, str) or len(spec.script) > 8000
                or not _integer(spec.max_duration_seconds, 7200) or spec.max_duration_seconds < 1
                or not isinstance(spec.scenario, str) or spec.scenario not in SCENARIOS):
            raise DefiniteFailure("invalid_call_spec")
        key = (spec.call_id, spec.attempt_id)
        if key in self._submissions and self._submissions[key][0] != spec:
            raise DefiniteFailure("attempt_changed")
        if spec.scenario == "ambiguous":
            raise AmbiguousSubmission("submission_unknown")
        sid = "sim_" + hashlib.sha256(":".join(key).encode()).hexdigest()[:32]
        submission = Submission(sid)
        self._submissions[key] = (spec, submission)
        return submission

    def cancel(self, provider_call_id: str) -> bool:
        if (not isinstance(provider_call_id, str)
                or not re.fullmatch(r"sim_[a-f0-9]{32}", provider_call_id)):
            raise DefiniteFailure("unknown_provider_call")
        # Acknowledgment only: service waits for a signed terminal event.
        return True

    def events(self, spec: CallSpec, submission: Submission, tenant_id: str) -> list[ProviderEvent]:
        if submission != self.submit(spec) or not _identifier(tenant_id):
            raise DefiniteFailure("invalid_simulation_binding")
        terminal = {"timeout": "no_answer"}.get(spec.scenario, spec.scenario)
        statuses = ["initiated", "ringing"]
        if terminal == "completed":
            statuses.append("answered")
        statuses.append(terminal)
        return [ProviderEvent(spec.call_id, submission.provider_call_id, tenant_id,
                              submission.provider_call_id + ":" + str(index), index, status,
                              duration_seconds=min(30, spec.max_duration_seconds)
                              if status == "completed" else 0)
                for index, status in enumerate(statuses)]


def get_provider(name: str) -> SimulatedProvider:
    if name != "simulated":
        raise DefiniteFailure("provider_unavailable")
    return SimulatedProvider()
