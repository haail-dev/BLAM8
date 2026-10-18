"""Twilio carrier transport. Network use is an explicit runtime opt-in."""
from dataclasses import dataclass, field
import re
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from app.telephone_provider import CallSpec, Submission, DefiniteFailure, AmbiguousSubmission


def _sid(value, prefix):
    return isinstance(value, str) and bool(re.fullmatch(prefix + r"[0-9a-fA-F]{32}", value))


def _https(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DefiniteFailure("invalid_callback_url")
    return value


def _phone(value):
    return isinstance(value, str) and bool(re.fullmatch(r"\+[1-9][0-9]{7,14}", value))


def _query(url, **params):
    parsed = urlsplit(_https(url))
    return urlunsplit(parsed._replace(query=urlencode(parse_qsl(parsed.query) + list(params.items()))))


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str
    twiml_url: str
    status_callback_url: str


def verify_twilio_signature(url, params, signature, auth_token):
    """URL must be canonical configured public URL, never an unchecked Host."""
    if not auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
        return RequestValidator(auth_token).validate(_https(url), params, signature)
    except Exception:
        return False


def realtime_sip_twiml(project_id, correlation_id, dial_action_url=None, *, timeout=30,
                       sip_target=None, time_limit=600):
    """Correlation is an issued nonce, not caller identity or authorization."""
    if (not re.fullmatch(r"proj_[A-Za-z0-9_-]{1,100}", project_id or "")
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", correlation_id or "")
            or type(timeout) is not int or not 5 <= timeout <= 60
            or type(time_limit) is not int or not 1 <= time_limit <= 600):
        raise DefiniteFailure("invalid_sip_configuration")
    target = sip_target or f"sip:{project_id}@sip.api.openai.com;transport=tls"
    if target not in {f"sip:{project_id}@sip.api.openai.com;transport=tls",
                       f"sip:{project_id}@sip-eu.api.openai.com;transport=tls"}:
        raise DefiniteFailure("invalid_sip_configuration")
    response = Element("Response")
    attrs = {"timeout": str(timeout), "timeLimit": str(time_limit)}
    if dial_action_url:
        attrs.update(action=_https(dial_action_url), method="POST")
    dial = SubElement(response, "Dial", attrs)
    SubElement(dial, "Sip").text = target + "?" + urlencode({"x-blam8-correlation": correlation_id})
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


def transfer_twiml(target, action_url, timeout=20, status_callback_url=None, *, time_limit=600, screen_url=None):
    if (not _phone(target) or type(timeout) is not int or not 5 <= timeout <= 60
            or type(time_limit) is not int or not 1 <= time_limit <= 600):
        raise DefiniteFailure("invalid_transfer")
    response = Element("Response")
    dial = SubElement(response, "Dial", {"timeout": str(timeout), "timeLimit": str(time_limit), "answerOnBridge": "true",
                                        "action": _https(action_url), "method": "POST"})
    attrs = {}
    if status_callback_url:
        attrs = {"statusCallback": _https(status_callback_url), "statusCallbackMethod": "POST",
                 "statusCallbackEvent": "initiated ringing answered completed"}
    if screen_url:
        attrs.update(url=_https(screen_url), method="POST")
    SubElement(dial, "Number", attrs).text = target
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


def _gather_remaining(value):
    if type(value) is not int or not 0 <= value <= 600:
        raise DefiniteFailure("invalid_fallback_duration")
    return value


def capture_message_twiml(action_url, time_limit_remaining):
    """Idle timeout only; orchestration must enforce the carrier call deadline."""
    remaining = _gather_remaining(time_limit_remaining)
    response = Element("Response")
    if remaining >= 10:
        gather = SubElement(response, "Gather", {"action": _https(action_url), "method": "POST",
            "input": "speech", "speechTimeout": "auto", "timeout": str(min(10, remaining)),
            "actionOnEmptyResult": "true"})
        SubElement(gather, "Say").text = (
            "The handover was not completed. Please leave a short message after this prompt. "
            "You will hear it back before choosing whether to save it.")
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


def confirm_message_twiml(message, action_url, time_limit_remaining=60):
    """Read back untrusted text as escaped speech; DTMF confirmation is separate."""
    remaining = _gather_remaining(time_limit_remaining)
    if (not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000
            or any(ord(character) < 32 and character not in "\n\r\t" for character in message)):
        raise DefiniteFailure("invalid_fallback_message")
    response = Element("Response")
    if remaining >= 10:
        gather = SubElement(response, "Gather", {"action": _https(action_url), "method": "POST",
            "input": "dtmf", "numDigits": "1", "timeout": str(min(10, remaining)),
            "actionOnEmptyResult": "true"})
        SubElement(gather, "Say").text = (
            "Your message is: " + message + ". Press 1 to save this message, or press 2 to correct it.")
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


def handover_screen_twiml(action_url, remaining):
    """Run on the called operator leg before bridging; no input hangs up that leg."""
    remaining = _gather_remaining(remaining)
    response = Element("Response")
    if remaining >= 10:
        gather = SubElement(response, "Gather", {"action": _https(action_url), "method": "POST",
            "input": "dtmf", "numDigits": "1", "timeout": str(min(10, remaining)),
            "actionOnEmptyResult": "true"})
        SubElement(gather, "Say").text = (
            "A caller is waiting for a handover. Press 1 to accept this call. "
            "Otherwise, hang up to decline.")
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


class TwilioProvider:
    def __init__(self, config: TwilioConfig, *, transport=None, allow_network=False):
        if (not _sid(config.account_sid, "AC") or not config.auth_token
                or not _phone(config.from_number)):
            raise DefiniteFailure("invalid_provider_configuration")
        _https(config.twiml_url)
        _https(config.status_callback_url)
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise DefiniteFailure("invalid_offline_transport")
        self.config = config
        self.transport = transport
        self.allow_network = allow_network

    def _request(self, method, suffix, *, data=None, ambiguous=False, resource="Calls"):
        if self.transport is None and not self.allow_network:
            raise DefiniteFailure("live_calls_disabled")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.config.account_sid}/{resource}{suffix}"
        try:
            with httpx.Client(transport=self.transport, timeout=15, follow_redirects=False,
                              trust_env=False, auth=(self.config.account_sid, self.config.auth_token)) as client:
                response = client.request(method, url, **({"content": data,
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"}}
                    if isinstance(data, str) else {"data": data}))
        except httpx.HTTPError:
            error = AmbiguousSubmission if ambiguous else DefiniteFailure
            raise error("provider_response_unknown") from None
        if 400 <= response.status_code < 500:
            raise DefiniteFailure("provider_rejected")
        if not 200 <= response.status_code < 300:
            error = AmbiguousSubmission if ambiguous else DefiniteFailure
            raise error("provider_response_unknown")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            error = AmbiguousSubmission if ambiguous else DefiniteFailure
            raise error("provider_response_unknown") from None

    def submit(self, spec: CallSpec) -> Submission:
        if (not _phone(spec.destination) or type(spec.max_duration_seconds) is not int
                or not 1 <= spec.max_duration_seconds <= 600):
            raise DefiniteFailure("invalid_call_spec")
        data = {"To": spec.destination, "From": self.config.from_number,
                "Url": _query(self.config.twiml_url, call_id=spec.call_id, attempt_id=spec.attempt_id),
                "Method": "POST", "StatusCallback": _query(self.config.status_callback_url,
                    call_id=spec.call_id, attempt_id=spec.attempt_id),
                "StatusCallbackMethod": "POST", "StatusCallbackEvent": "initiated ringing answered completed",
                "TimeLimit": str(spec.max_duration_seconds), "Timeout": "30", "Record": "false"}
        # Twilio expects repeated form keys for progress events.
        form = urlencode([(key, value) for key, value in data.items() if key != "StatusCallbackEvent"]
                         + [("StatusCallbackEvent", value) for value in data["StatusCallbackEvent"].split()])
        result = self._request("POST", ".json", data=form, ambiguous=True)
        if not _sid(result.get("sid"), "CA") or result.get("account_sid") != self.config.account_sid:
            raise AmbiguousSubmission("provider_binding_unknown")
        return Submission(result["sid"], simulated=False)

    def cancel(self, provider_call_id):
        if not _sid(provider_call_id, "CA"):
            raise DefiniteFailure("invalid_provider_call")
        current = self._request("GET", f"/{provider_call_id}.json")
        if current.get("sid") != provider_call_id or current.get("account_sid") != self.config.account_sid:
            raise DefiniteFailure("provider_binding_mismatch")
        status = current.get("status")
        if status in {"completed", "canceled", "failed", "busy", "no-answer"}:
            return True
        if status not in {"queued", "ringing", "in-progress"}:
            raise DefiniteFailure("cancellation_unknown")
        self._request("POST", f"/{provider_call_id}.json",
                      data={"Status": "completed" if status == "in-progress" else "canceled"}, ambiguous=True)
        return True  # command accepted; signed progress determines terminal state

    def transfer(self, provider_call_id, target, action_url, timeout=20, status_callback_url=None, *, time_limit=600, screen_url=None):
        if not _sid(provider_call_id, "CA"):
            raise DefiniteFailure("invalid_provider_call")
        twiml = transfer_twiml(target, action_url, timeout, status_callback_url,
                               time_limit=time_limit, screen_url=screen_url)
        self._request("POST", f"/{provider_call_id}.json", data={"Twiml": twiml}, ambiguous=True)
        return True

    def verify_incoming_number(self, number):
        return self._owned_number(number) is not None

    def _owned_number(self, number):
        if not _phone(number):
            return None
        result = self._request("GET", ".json?" + urlencode({"PhoneNumber": number, "PageSize": 100}),
                               resource="IncomingPhoneNumbers")
        return next((row for row in result.get("incoming_phone_numbers", [])
                     if isinstance(row, dict) and row.get("phone_number") == number
                     and row.get("account_sid") == self.config.account_sid
                     and isinstance(row.get("capabilities"), dict)
                     and row["capabilities"].get("voice") is True), None)

    def verify_caller_id(self, number):
        if not _phone(number):
            return False
        if self.verify_incoming_number(number):
            return True
        result = self._request("GET", ".json?" + urlencode({"PhoneNumber": number, "PageSize": 100}),
                               resource="OutgoingCallerIds")
        return any(row.get("phone_number") == number and row.get("account_sid") == self.config.account_sid
                   for row in result.get("outgoing_caller_ids", []) if isinstance(row, dict))

    def limit_duration(self, provider_call_id, total_seconds):
        if not _sid(provider_call_id, "CA") or type(total_seconds) is not int or not 1 <= total_seconds <= 600:
            raise DefiniteFailure("invalid_call_duration")
        current = self._request("GET", f"/{provider_call_id}.json")
        if current.get("sid") != provider_call_id or current.get("account_sid") != self.config.account_sid:
            raise DefiniteFailure("provider_binding_mismatch")
        self._request("POST", f"/{provider_call_id}.json", data={"TimeLimit": str(total_seconds)}, ambiguous=True)
        return True

    def configure_number(self, number):
        """Explicit provisioning action; only voice routing fields are changed."""
        owned = self._owned_number(number)
        if not owned or not _sid(owned.get("sid"), "PN"):
            raise DefiniteFailure("incoming_number_unverified")
        self._request("POST", f"/{owned['sid']}.json", resource="IncomingPhoneNumbers",
            data={"VoiceUrl": self.config.twiml_url, "VoiceMethod": "POST",
                  "StatusCallback": self.config.status_callback_url, "StatusCallbackMethod": "POST"},
            ambiguous=True)
        return True
