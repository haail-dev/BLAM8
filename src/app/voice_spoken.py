"""The spoken filter for live voice (docs/voice-live-seed.md step 3).

The business concierge answers in STRUCTURED JSON — {"reply": "...", "links":
[...], "action": {...}} — and on the voice surface those deltas stream straight
towards a text-to-speech voice. Only the `reply` string may ever be spoken:
links, action parameters and draft bodies are machinery, and a voice reading
JSON aloud is the failure this module exists to prevent (OurTab's
createSpokenFilter, ported to BLAM8's action representation).

ReplyExtractor is an incremental scanner over the delta stream: it tokenises
just enough JSON to know when it is inside the top-level "reply" string value,
un-escapes that value as it arrives, and emits it in speakable pieces. Tags
split across arbitrary chunk boundaries are the whole problem, so the tests
(tests/test_voice_live.py) feed every case at chunk strides 1/3/7 and demand
identical output — same contract as the donor's unit tests.

Pure logic, no I/O, no app imports.
"""
from __future__ import annotations

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t",
            "r": "\r", "b": "", "f": ""}


class ReplyExtractor:
    """Feed JSON text deltas with push(); the top-level "reply" string value is
    un-escaped and handed to `emit` as it arrives. end() flushes. One instance
    per JSON document — a tool loop makes one document per round, so the caller
    resets the extractor at each round boundary."""

    def __init__(self, emit):
        self._emit = emit
        self._depth = 0
        self._in_string = False
        self._is_key = False          # the string now open is an object key
        self._key = ""                # accumulates the current key's text
        self._last_key = ""           # the most recent completed top-level key
        self._emitting = False        # inside the top-level "reply" value
        self._done = False            # reply value fully emitted — ignore the rest
        self._escape = False          # previous char was a backslash
        self._unicode = ""            # partial \uXXXX hex digits
        self._pending_high = ""       # a high surrogate waiting for its pair
        self._buf = []                # emit buffer for the current push()
        self._expect_value = False    # at depth 1: the next token is a VALUE

    def push(self, text: str) -> None:
        if self._done or not text:
            return
        for ch in text:
            self._char(ch)
        if self._buf:
            self._emit("".join(self._buf))
            self._buf = []

    def end(self) -> None:
        """Flush anything still buffered (an unterminated value at stream end
        is spoken as far as it got, never dropped mid-sentence)."""
        if self._pending_high:
            self._buf.append(self._pending_high)
            self._pending_high = ""
        if self._buf:
            self._emit("".join(self._buf))
            self._buf = []
        self._done = True

    # ---- internals ----------------------------------------------------------
    def _out(self, text: str) -> None:
        if self._emitting and text:
            self._buf.append(text)

    def _finish_escape(self, decoded: str) -> None:
        # Surrogate pairs arrive as two \uXXXX escapes; join them, and flush a
        # lone high surrogate rather than holding it forever. Only the spoken
        # value needs any of this — escapes elsewhere are simply discarded.
        if not self._emitting:
            return
        if self._pending_high:
            if len(decoded) == 1 and "\udc00" <= decoded <= "\udfff":
                pair = (0x10000 + ((ord(self._pending_high) - 0xD800) << 10)
                        + (ord(decoded) - 0xDC00))
                self._out(chr(pair))
            else:
                self._out(self._pending_high)
                self._out(decoded)
            self._pending_high = ""
        elif len(decoded) == 1 and "\ud800" <= decoded <= "\udbff":
            self._pending_high = decoded
        else:
            self._out(decoded)

    def _char(self, ch: str) -> None:
        if self._done:
            return
        if self._escape:
            self._escape = False
            if ch == "u":
                self._unicode = "u"
            else:
                decoded = _ESCAPES.get(ch, ch)
                if self._is_key:
                    self._key += decoded
                else:
                    self._finish_escape(decoded)
            return
        if self._unicode:
            self._unicode += ch
            if len(self._unicode) == 5:      # 'u' + 4 hex digits
                try:
                    decoded = chr(int(self._unicode[1:], 16))
                except ValueError:
                    decoded = ""
                self._unicode = ""
                if self._is_key:
                    self._key += decoded
                else:
                    self._finish_escape(decoded)
            return

        if self._in_string:
            if ch == "\\":
                self._escape = True
                return
            if ch == '"':
                self._in_string = False
                if self._is_key:
                    self._last_key = self._key
                    self._is_key = False
                elif self._emitting:
                    self._emitting = False
                    self._done = True     # reply finished — nothing else speaks
                return
            if self._is_key:
                self._key += ch
            else:
                self._out(ch)
            return

        # Outside any string: structure only.
        if ch == '"':
            self._in_string = True
            if self._depth == 1 and not self._expect_value:
                self._is_key = True
                self._key = ""
            elif self._depth == 1 and self._expect_value and self._last_key == "reply":
                self._emitting = True
            return
        if ch == "{" or ch == "[":
            self._depth += 1
            if self._depth == 1:
                self._expect_value = False
            return
        if ch == "}" or ch == "]":
            self._depth = max(0, self._depth - 1)
            if self._depth == 0:
                self._done = True
            return
        if ch == ":" and self._depth == 1:
            self._expect_value = True
            return
        if ch == "," and self._depth == 1:
            self._expect_value = False
            return
