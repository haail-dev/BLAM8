"""The browser tests must wait for Chromium's DevToolsActivePort to be complete, not merely present."""
from pathlib import Path
import threading
import time

import pytest

from tests.test_knowledge_guidance_browser import devtools_port

COMPLETE = '41234\n/devtools/browser/0b0b2b7e-1111-2222-3333-444455556666'


def write_later(path, steps):
    """Play Chromium: each (delay, content) step rewrites the file after its delay."""
    def run():
        for delay, content in steps:
            time.sleep(delay)
            path.write_text(content)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker


def test_waits_for_a_file_that_exists_but_is_still_empty(tmp_path):
    # The CI failure: the file is visible, then read before Chromium wrote anything into it.
    write_later(tmp_path / 'DevToolsActivePort', [(0.05, ''), (0.4, COMPLETE)])
    started = time.monotonic()
    assert devtools_port(tmp_path, timeout=5) == '41234'
    assert time.monotonic() - started >= 0.4


def test_waits_for_the_websocket_line_after_a_partial_write(tmp_path):
    write_later(tmp_path / 'DevToolsActivePort', [(0.05, '41234\n'), (0.4, COMPLETE)])
    started = time.monotonic()
    assert devtools_port(tmp_path, timeout=5) == '41234'
    assert time.monotonic() - started >= 0.4


def test_retries_when_the_file_is_unreadable_while_chromium_writes(tmp_path, monkeypatch):
    # Windows refuses reads while Chromium still has the file open for writing.
    (tmp_path / 'DevToolsActivePort').write_text(COMPLETE)
    real, calls = Path.read_text, []

    def flaky(self, *args, **kwargs):
        calls.append(self)
        if len(calls) <= 3:
            raise PermissionError('sharing violation')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', flaky)
    assert devtools_port(tmp_path, timeout=5) == '41234'
    assert len(calls) == 4


def test_a_missing_file_times_out_with_a_clear_message(tmp_path):
    with pytest.raises(AssertionError, match='debugging endpoint did not start'):
        devtools_port(tmp_path, timeout=0.2)
