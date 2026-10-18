"""OpenAI Realtime SIP controls and bounded sideband receiver; no outbound create."""
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import json
import re
from urllib.parse import quote

import httpx

from app.telephone_provider import DefiniteFailure, AmbiguousSubmission


@dataclass(frozen=True)
class RealtimeConfig:
    api_key: str = field(repr=False)
    model: str
    webhook_secret: str = field(default="", repr=False)


def _call_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise DefiniteFailure("invalid_realtime_call")
    return value


def verify_incoming_webhook(raw_body, headers, secret):
    """Official SDK validates original bytes and timestamp before parsing."""
    if not secret or not isinstance(raw_body, bytes) or len(raw_body) > 65536:
        raise DefiniteFailure("invalid_openai_webhook")
    try:
        from openai import OpenAI
        # Verification is local; this client never performs an HTTP request.
        with OpenAI(api_key="local-signature-verification", max_retries=0) as client:
            client.webhooks.unwrap(raw_body, headers, secret=secret)
        body = json.loads(raw_body)
        if body.get("type") != "realtime.call.incoming":
            raise ValueError()
        data = body["data"]
        _call_id(data["call_id"])
        if not isinstance(data.get("sip_headers"), list) or len(data["sip_headers"]) > 100:
            raise ValueError()
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
               or not 1 <= len(item["name"]) <= 128 or not isinstance(item.get("value"), str)
               or len(item["value"]) > 2048 for item in data["sip_headers"]):
            raise ValueError()
        return body
    except Exception:
        raise DefiniteFailure("invalid_openai_webhook") from None


class RealtimeClient:
    def __init__(self, config: RealtimeConfig, *, transport=None, allow_network=False):
        if not config.api_key or not isinstance(config.model, str) or not config.model.strip():
            raise DefiniteFailure("invalid_realtime_configuration")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise DefiniteFailure("invalid_offline_transport")
        self.config = config
        self.transport = transport
        self.allow_network = allow_network

    def _control(self, call_id, action, payload=None):
        _call_id(call_id)
        if self.transport is None and not self.allow_network:
            raise DefiniteFailure("live_calls_disabled")
        try:
            with httpx.Client(transport=self.transport, timeout=15, follow_redirects=False,
                              trust_env=False, headers={"Authorization": "Bearer " + self.config.api_key}) as client:
                response = client.post(f"https://api.openai.com/v1/realtime/calls/{call_id}/{action}",
                                       **({"json": payload} if payload is not None else {}))
        except httpx.HTTPError:
            raise AmbiguousSubmission("realtime_control_unknown") from None
        if 400 <= response.status_code < 500:
            raise DefiniteFailure("realtime_control_rejected")
        if response.status_code != 200:
            raise AmbiguousSubmission("realtime_control_unknown")
        return True

    def accept(self, call_id, instructions, tools=None):
        if not isinstance(instructions, str) or not 1 <= len(instructions) <= 16000:
            raise DefiniteFailure("invalid_realtime_instructions")
        return self._control(call_id, "accept", {"type": "realtime", "model": self.config.model,
                             "instructions": instructions, "tools": tools or []})

    def reject(self, call_id, status_code=486):
        if type(status_code) is not int or not 300 <= status_code <= 699:
            raise DefiniteFailure("invalid_realtime_rejection")
        return self._control(call_id, "reject", {"status_code": status_code})

    def refer(self, call_id, target_uri):
        # Root must authorize the configured target and exclude forwarding loops.
        if not isinstance(target_uri, str) or not re.fullmatch(r"tel:\+[1-9][0-9]{7,14}", target_uri):
            raise DefiniteFailure("invalid_transfer")
        return self._control(call_id, "refer", {"target_uri": target_uri})

    def hangup(self, call_id):
        return self._control(call_id, "hangup")

    @asynccontextmanager
    async def sideband(self, call_id, *, connect=None):
        _call_id(call_id)
        if connect is None:
            if not self.allow_network:
                raise DefiniteFailure("live_calls_disabled")
            from websockets.asyncio.client import connect
        async with connect("wss://api.openai.com/v1/realtime?call_id=" + quote(call_id, safe=""),
                           additional_headers={"Authorization": "Bearer " + self.config.api_key},
                           max_size=65536, open_timeout=10, ping_interval=20, close_timeout=5) as socket:
            yield socket
