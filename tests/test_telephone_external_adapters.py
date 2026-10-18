"""HTTP/signature/socket contract tests; never contact either provider."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from twilio.request_validator import RequestValidator

from app.telephone_provider import AmbiguousSubmission, CallSpec, DefiniteFailure
from app.telephone_twilio import (TwilioConfig, TwilioProvider, realtime_sip_twiml,
                                 transfer_twiml, verify_twilio_signature,
                                 capture_message_twiml, confirm_message_twiml, handover_screen_twiml)
from app.telephone_realtime import RealtimeConfig, RealtimeClient, verify_incoming_webhook

ACCOUNT = "AC" + "1" * 32
CALL = "CA" + "2" * 32
BASE = "https://telephone.example/webhook/telephone/"
CONFIG = TwilioConfig(ACCOUNT, "offline-secret", "+442079460100", BASE + "twilio/voice", BASE + "twilio/status")
SPEC = CallSpec("request-1", "attempt-1", "+442079460101", "Requested callback", 60)


def test_live_network_is_disabled_and_secrets_not_represented():
    with pytest.raises(DefiniteFailure, match="live_calls_disabled"):
        TwilioProvider(CONFIG).submit(SPEC)
    with pytest.raises(DefiniteFailure, match="live_calls_disabled"):
        RealtimeClient(RealtimeConfig("offline-key", "configured-model")).accept("rtc_call", "Instructions")
    assert "offline-secret" not in repr(CONFIG)
    assert "offline-key" not in repr(RealtimeConfig("offline-key", "configured-model"))


def test_outbound_request_is_single_form_post_with_bounded_call_and_correlated_callbacks():
    seen = []
    def handler(request):
        seen.append(request)
        form = parse_qs(request.content.decode())
        assert request.method == "POST"
        assert request.url == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Calls.json"
        assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
        assert form["TimeLimit"] == ["60"] and form["Record"] == ["false"]
        assert "attempt_id=attempt-1" in form["Url"][0]
        assert form["From"] == [CONFIG.from_number] and form["To"] == [SPEC.destination]
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        return httpx.Response(201, json={"sid": CALL, "account_sid": ACCOUNT})
    result = TwilioProvider(CONFIG, transport=httpx.MockTransport(handler)).submit(SPEC)
    assert result.provider_call_id == CALL and result.simulated is False
    assert len(seen) == 1


@pytest.mark.parametrize("status,error", [(400, DefiniteFailure), (429, DefiniteFailure),
                                         (500, AmbiguousSubmission), (302, AmbiguousSubmission)])
def test_submit_rejection_vs_unknown_never_retries(status, error):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"message": "sensitive-provider-prose"})
    with pytest.raises(error) as raised:
        TwilioProvider(CONFIG, transport=httpx.MockTransport(handler)).submit(SPEC)
    assert len(seen) == 1 and "sensitive" not in str(raised.value)


def test_timeout_or_wrong_account_is_unknown_not_safe_retry():
    def timeout(request):
        raise httpx.ReadTimeout("sensitive", request=request)
    with pytest.raises(AmbiguousSubmission):
        TwilioProvider(CONFIG, transport=httpx.MockTransport(timeout)).submit(SPEC)
    with pytest.raises(AmbiguousSubmission, match="provider_binding_unknown"):
        TwilioProvider(CONFIG, transport=httpx.MockTransport(lambda r: httpx.Response(201,
            json={"sid": CALL, "account_sid": "AC" + "3" * 32}))).submit(SPEC)


@pytest.mark.parametrize("status,command", [("queued", "canceled"), ("ringing", "canceled"),
                                           ("in-progress", "completed")])
def test_cancel_chooses_provider_command_and_only_returns_ack(status, command):
    seen = []
    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            assert parse_qs(request.content.decode())["Status"] == [command]
        return httpx.Response(200, json={"sid": CALL, "account_sid": ACCOUNT, "status": status})
    assert TwilioProvider(CONFIG, transport=httpx.MockTransport(handler)).cancel(CALL)
    assert seen == ["GET", "POST"]


def test_official_twilio_signature_binds_canonical_url_all_fields_and_account():
    params = {"AccountSid": ACCOUNT, "CallSid": CALL, "FutureField": "yes"}
    url = BASE + "twilio/status?call_id=request-1"
    signature = RequestValidator(CONFIG.auth_token).compute_signature(url, params)
    assert verify_twilio_signature(url, params, signature, CONFIG.auth_token)
    assert not verify_twilio_signature(url.replace("telephone.example", "forged.example"), params,
                                       signature, CONFIG.auth_token)
    assert not verify_twilio_signature(url, dict(params, FutureField="changed"), signature, CONFIG.auth_token)


def test_twiml_only_allows_fixed_project_sip_and_explicit_transfer_callbacks():
    xml = fromstring(realtime_sip_twiml("proj_example", "a" * 43, BASE + "twilio/transfer", time_limit=75))
    assert xml.find("Dial").attrib["timeLimit"] == "75"
    assert "sip:proj_example@sip.api.openai.com;transport=tls?x-blam8-correlation=" in xml.find("Dial/Sip").text
    with pytest.raises(DefiniteFailure):
        realtime_sip_twiml("proj_example", "a" * 43, sip_target="sip:attacker@example.com")
    transfer = fromstring(transfer_twiml("+442079460102", BASE + "twilio/transfer", 20,
                                        BASE + "twilio/status"))
    assert transfer.find("Dial").attrib["answerOnBridge"] == "true"
    assert transfer.find("Dial").attrib["timeLimit"] == "600"
    assert transfer.find("Dial/Number").attrib["statusCallbackEvent"] == "initiated ringing answered completed"
    assert transfer.find("Dial/Number").text == "+442079460102"


@pytest.mark.parametrize("duration", [0, 601, True, "60"])
def test_carrier_duration_bounds_cannot_be_omitted_or_confused(duration):
    with pytest.raises(DefiniteFailure):
        realtime_sip_twiml("proj_example", "a" * 43, time_limit=duration)
    with pytest.raises(DefiniteFailure):
        transfer_twiml("+442079460102", BASE + "twilio/transfer", time_limit=duration)


def test_failed_handover_gather_separates_message_capture_and_explicit_confirmation():
    capture = fromstring(capture_message_twiml(BASE + "twilio/fallback-message", 50))
    assert capture.find("Gather").attrib == {"action": BASE + "twilio/fallback-message", "method": "POST",
        "input": "speech", "speechTimeout": "auto", "timeout": "10", "actionOnEmptyResult": "true"}
    message = '<Hangup/> & "Please call back"'
    raw = confirm_message_twiml(message, BASE + "twilio/fallback-confirm?revision=1&nonce=example", 50)
    confirm = fromstring(raw)
    assert "<Hangup/> &" not in raw
    assert message in confirm.find("Gather/Say").text
    assert confirm.find("Gather").attrib["numDigits"] == "1"
    assert confirm.find("Gather").attrib["input"] == "dtmf"
    assert "Press 1 to save" in confirm.find("Gather/Say").text
    assert len(confirm.findall("Hangup")) == 1 and confirm.find("Gather/Hangup") is None


@pytest.mark.parametrize("remaining", [0, 1, 9])
def test_fallback_has_no_new_prompt_when_time_is_exhausted(remaining):
    assert fromstring(capture_message_twiml(BASE + "twilio/fallback-message", remaining)).find("Gather") is None
    assert fromstring(confirm_message_twiml("message", BASE + "twilio/fallback-confirm", remaining)).find("Gather") is None


@pytest.mark.parametrize("remaining", [-1, 601, True, "20"])
def test_fallback_duration_type_and_range_bounds(remaining):
    with pytest.raises(DefiniteFailure):
        capture_message_twiml(BASE + "twilio/fallback-message", remaining)


@pytest.mark.parametrize("message", ["", "x" * 2001, "unsafe\x00", None])
def test_fallback_readback_is_bounded_and_xml_safe(message):
    with pytest.raises(DefiniteFailure):
        confirm_message_twiml(message, BASE + "twilio/fallback-confirm")


def test_operator_screen_runs_before_bridge_and_requires_separate_dtmf_acceptance():
    screen_url = BASE + "twilio/handover-screen?transfer_id=transfer-1&stage=operator"
    xml = fromstring(transfer_twiml("+442079460102", BASE + "twilio/transfer", screen_url=screen_url))
    assert xml.find("Dial").attrib["answerOnBridge"] == "true"
    assert xml.find("Dial/Number").attrib == {"url": screen_url, "method": "POST"}
    prompt = fromstring(handover_screen_twiml(BASE + "twilio/handover-accept?transfer_id=transfer-1", 25))
    assert prompt.find("Gather").attrib["input"] == "dtmf"
    assert prompt.find("Gather").attrib["numDigits"] == "1"
    assert prompt.find("Gather").attrib["timeout"] == "10"
    assert "Press 1 to accept" in prompt.find("Gather/Say").text
    assert prompt.find("Hangup") is not None
    assert prompt.find("Dial") is None and prompt.find("Record") is None


@pytest.mark.parametrize("remaining", [0, 5, 9])
def test_operator_screen_without_time_declines_child_leg(remaining):
    xml = fromstring(handover_screen_twiml(BASE + "twilio/handover-accept", remaining))
    assert xml.find("Gather") is None and xml.find("Hangup") is not None


def test_transfer_http_body_keeps_operator_screen_and_duration_bounds():
    def handler(request):
        form = parse_qs(request.content.decode())
        xml = fromstring(form["Twiml"][0])
        assert xml.find("Dial/Number").attrib["url"] == BASE + "twilio/handover-screen"
        assert xml.find("Dial").attrib["timeLimit"] == "40"
        return httpx.Response(200, json={"sid": CALL, "account_sid": ACCOUNT})
    assert TwilioProvider(CONFIG, transport=httpx.MockTransport(handler)).transfer(CALL, "+442079460102",
        BASE + "twilio/transfer", time_limit=40, screen_url=BASE + "twilio/handover-screen")


@pytest.mark.parametrize("owned,verified,expected", [(True, False, True), (False, True, True),
                                                   (False, False, False)])
def test_provider_number_verification_is_get_only_and_exact_account(owned, verified, expected):
    def handler(request):
        assert request.method == "GET"
        if "IncomingPhoneNumbers" in request.url.path:
            return httpx.Response(200, json={"incoming_phone_numbers": [{"phone_number": CONFIG.from_number,
                "account_sid": ACCOUNT, "capabilities": {"voice": owned}}]})
        return httpx.Response(200, json={"outgoing_caller_ids": ([{"phone_number": CONFIG.from_number,
                "account_sid": ACCOUNT}] if verified else [])})
    provider = TwilioProvider(CONFIG, transport=httpx.MockTransport(handler))
    assert provider.verify_incoming_number(CONFIG.from_number) is owned
    assert provider.verify_caller_id(CONFIG.from_number) is expected


def test_realtime_control_uses_existing_call_id_not_outbound_creation():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path.startswith("/v1/realtime/calls/rtc_call/")
        if request.url.path.endswith("accept"):
            body = json.loads(request.content)
            assert body["type"] == "realtime" and body["model"] == "configured-model"
        return httpx.Response(200)
    client = RealtimeClient(RealtimeConfig("offline-key", "configured-model"), transport=httpx.MockTransport(handler))
    assert client.accept("rtc_call", "Approved bounded instructions")
    assert client.reject("rtc_call")
    assert client.refer("rtc_call", "tel:+442079460102")
    assert client.hangup("rtc_call")
    assert len(seen) == 4 and not seen[-1].content


def test_total_duration_update_and_explicit_number_configuration_do_not_change_sms():
    seen = []
    def handler(request):
        seen.append(request)
        if request.method == "POST":
            form = parse_qs(request.content.decode())
            if "IncomingPhoneNumbers" in request.url.path:
                assert set(form) == {"VoiceUrl", "VoiceMethod", "StatusCallback", "StatusCallbackMethod"}
                assert form["VoiceUrl"] == [CONFIG.twiml_url]
                assert "SmsUrl" not in form
            else:
                assert form == {"TimeLimit": ["95"]}
            return httpx.Response(200, json={})
        if "IncomingPhoneNumbers" in request.url.path:
            return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN" + "4" * 32,
                "phone_number": CONFIG.from_number, "account_sid": ACCOUNT, "capabilities": {"voice": True}}]})
        return httpx.Response(200, json={"sid": CALL, "account_sid": ACCOUNT})
    provider = TwilioProvider(CONFIG, transport=httpx.MockTransport(handler))
    assert provider.limit_duration(CALL, 95)
    assert provider.configure_number(CONFIG.from_number)
    assert [item.method for item in seen] == ["GET", "POST", "GET", "POST"]


@pytest.mark.parametrize("duration", [0, 601, True])
def test_total_call_duration_update_rejects_invalid_bounds_before_transport(duration):
    with pytest.raises(DefiniteFailure, match="invalid_call_duration"):
        TwilioProvider(CONFIG).limit_duration(CALL, duration)


@pytest.mark.parametrize("sip_headers", [[], [123], [{"name": "X-Correlation", "value": 12}],
    [{"name": "x" * 129, "value": "value"}]])
def test_official_openai_webhook_signature_and_timestamp_verification(sip_headers):
    raw = json.dumps({"id": "evt_incoming", "object": "event", "created_at": int(time.time()),
        "type": "realtime.call.incoming", "data": {"call_id": "rtc_call", "sip_headers": sip_headers}}).encode()
    secret_bytes = b"test-webhook-secret-at-least-32-bytes"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(secret_bytes, b"wh_test." + timestamp.encode() + b"." + raw,
                                         hashlib.sha256).digest()).decode()
    headers = {"webhook-id": "wh_test", "webhook-timestamp": timestamp, "webhook-signature": "v1," + signature}
    if sip_headers:
        with pytest.raises(DefiniteFailure, match="invalid_openai_webhook"):
            verify_incoming_webhook(raw, headers, secret)
        return
    assert verify_incoming_webhook(raw, headers, secret)["data"]["call_id"] == "rtc_call"
    with pytest.raises(DefiniteFailure, match="invalid_openai_webhook"):
        verify_incoming_webhook(raw + b" ", headers, secret)
    with pytest.raises(DefiniteFailure, match="invalid_openai_webhook"):
        verify_incoming_webhook(raw, dict(headers, **{"webhook-timestamp": "1"}), secret)


def test_sideband_context_uses_authenticated_existing_session_and_bounded_frames():
    class Socket:
        async def send(self, raw):
            assert json.loads(raw)["type"] == "response.create"
    class Context:
        async def __aenter__(self):
            return Socket()
        async def __aexit__(self, *args):
            return False
    def connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/realtime?call_id=rtc_call"
        assert kwargs["max_size"] == 65536 and kwargs["open_timeout"] == 10
        assert kwargs["additional_headers"]["Authorization"] == "Bearer offline-key"
        return Context()
    async def check():
        client = RealtimeClient(RealtimeConfig("offline-key", "configured-model"))
        async with client.sideband("rtc_call", connect=connect) as socket:
            await socket.send(json.dumps({"type": "response.create"}))
        with pytest.raises(DefiniteFailure, match="live_calls_disabled"):
            async with client.sideband("rtc_call"):
                pass
    asyncio.run(check())
