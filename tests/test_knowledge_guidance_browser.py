"""Native offline form roundtrip; only the guide's fetch transport is substituted."""
import asyncio
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from jinja2 import Environment, FileSystemLoader

from app import knowledge_guidance as guide, knowledge_review
from tests.test_knowledge_review import context, entry


ROOT = Path(__file__).resolve().parents[1]


def chromium_binary():
    configured = os.environ.get('BLAM8_TEST_CHROMIUM_BIN')
    if configured:
        assert Path(configured).is_file(), 'BLAM8_TEST_CHROMIUM_BIN must name an existing executable.'
        return configured
    candidates = [
        shutil.which(name) for name in ('google-chrome', 'google-chrome-stable',
                                        'chromium', 'chromium-browser', 'chrome')]
    for base in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(base):
            candidates.append(str(Path(os.environ[base]) / 'Google/Chrome/Application/chrome.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise AssertionError('Chromium/Chrome is required; set BLAM8_TEST_CHROMIUM_BIN to its executable.')


def devtools_port(profile, timeout=15):
    """Return the remote-debugging port of a Chromium started with --remote-debugging-port=0.

    Chromium makes DevToolsActivePort visible before it has finished writing it (line one is the
    port, line two the browser websocket path), so the file existing is not readiness: an early
    read sees an empty file or, on Windows, a sharing violation. Wait for the complete file."""
    port_file = Path(profile) / 'DevToolsActivePort'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = port_file.read_text().splitlines()
        except OSError:  # not created yet, or still held open by Chromium while it writes
            lines = []
        if len(lines) >= 2 and lines[0].isdigit() and lines[1]:
            return lines[0]
        time.sleep(0.05)
    raise AssertionError('Chromium debugging endpoint did not start')


def test_overlap_checkbox_roundtrip_in_native_chromium(tmp_path):
    first = entry(label='Services', content='We handle resident requests and keep owners informed.')
    ctx, row = context([first, entry('two', 'Fees', 'Management costs £95 each month.')],
                       brief=first['content'])
    row['payload']['questions'] = []
    form = {'version': ctx['version']}
    initial = {**guide.present(ctx, row, form), 'patches': {}}
    responses = [initial]
    for message in ('I’d like to edit this note before saving it.',
                    'Keep this note after I remove the repeated sentence.'):
        responses.append(guide.apply(ctx, row, form, initial['focus_hash'], dict(
            intent='keep', reason=message, answer='', target='', explanation='Recorded as a keep.')))
    first_confirmation = len(responses)
    for intent in ('keep', 'keep_both', 'keep'):
        shown = guide.present(ctx, row, form)
        result = guide.apply(ctx, row, form, shown['focus_hash'], dict(
            intent=intent, reason='', answer='', target='', explanation=''), confirmed_note_decision=True)
        form.update(result['patches'])
        responses.append(result)
    overlap = responses[first_confirmation]['focus']
    assert overlap['kind'] == 'overlap' and responses[first_confirmation + 1]['focus']['id'] == 'two'
    match_name = 'match_' + overlap['id']
    confirmation_request = len(responses) - 1
    env = Environment(loader=FileSystemLoader(ROOT / 'src/app/templates'), autoescape=True)
    markup = env.get_template('_knowledge_review.html').render(
        review=knowledge_review.review_view(ctx, row, {'version': ctx['version']}),
        csrf_token=lambda request: 'offline-csrf', request={})
    controller = (ROOT / 'src/app/static/knowledge_guidance.js').read_text(encoding='utf-8')
    assertions = (ROOT / 'tests/replays/knowledge_guidance_native.js').read_text(encoding='utf-8')
    page = tmp_path / 'review.html'
    page.write_text('<!doctype html><html><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'script-src \'unsafe-inline\'; connect-src \'none\'">'
        '</head><body>' + markup + '<pre id="native-result">PENDING</pre><script>'
        + 'window.nativeFixture=' + json.dumps({'responses': responses, 'matchName': match_name,
                                               'confirmationRequest': confirmation_request})
        + ';</script><script>' + controller + '</script><script>' + assertions
        + '</script></body></html>', encoding='utf-8')
    result = subprocess.run([
        chromium_binary(), '--headless=new', '--no-sandbox', '--disable-gpu',
        '--disable-background-networking', '--disable-component-update', '--disable-sync',
        '--disable-default-apps', '--no-first-run', '--no-default-browser-check',
        '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--user-data-dir=' + str(tmp_path / 'browser-profile'),
        '--virtual-time-budget=5000', '--dump-dom', page.as_uri(),
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    report = re.search(r'<pre id="native-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
    assert report and 'data-status="PASS"' in report.group(0), report.group(1) if report else result.stdout[-2000:]
    captured = json.loads(unescape(report.group(1)))['requests']
    # Replay the browser's actual serialized form/hash through the real backend.
    request = captured[confirmation_request]
    _, current = guide.current(ctx, row, request['form'])
    assert current['kind'] == 'note' and current['id'] == 'two'
    assert request['focus_hash'] == guide.identity(current)
    accepted = asyncio.run(guide.turn('offline-tenant', ctx, row, request['form'],
                                      request['question'], request['focus_hash']))
    assert accepted['patches'] == {'action_two': 'keep'}
    assert row['state'] == 'pending'
