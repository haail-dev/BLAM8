"""Native guide-to-Save roundtrip: real controllers and planner over loopback, no model."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import parse_qsl

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from app import knowledge_guidance as guide, knowledge_review
from tests.test_knowledge_guidance_browser import chromium_binary
from tests.test_knowledge_review import PARAGRAPHS, context, entry

ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = {'knowledge_source_review.js', 'knowledge_guidance.js'}

# The real guide controller reviews the batch one decision at a time, then uses its
# own Save reviewed decisions path, which is a native form submission.
DRIVER = r'''
window.addEventListener('DOMContentLoaded', async () => {
  const report = payload => fetch('/browser-result', {method: 'POST', body: JSON.stringify(payload)});
  try {
    const assistant = window.Blam8KnowledgeGuide, seen = [];
    let data = await assistant.ask('');
    for (let turn = 0; turn < 40 && !data.save_ready; turn++) {
      const kind = data.focus.kind; seen.push(kind);
      const message = {note: 'Keep this note', overlap: 'Keep both notes', question: 'Leave this unknown'}[kind];
      if (!message) throw new Error('Unexpected guide item ' + kind + ': ' + (data.focus.text || ''));
      data = await assistant.ask(message);
    }
    if (!data.save_ready) throw new Error('The guide never offered Save; items seen: ' + seen.join(','));
    const form = document.querySelector('[data-knowledge-review]');
    const checkbox = form.querySelector('[name^=match_]');
    if (!checkbox || !checkbox.checked || new FormData(form).get(checkbox.name) !== 'keep_both')
      throw new Error('The guide acknowledgement did not tick the overlap checkbox');
    if (!form.elements['content_' + form.dataset.noteId].value.includes('\n\n'))
      throw new Error('textarea.value lost the paragraph breaks');
    setTimeout(() => report({status: 'FAIL', error: 'Save did not submit the form natively'}), 3000);
    assistant.save();
  } catch (error) { await report({status: 'FAIL', error: String(error)}); }
});
'''


def test_guide_overlap_acknowledgement_survives_the_native_save(tmp_path):
    note = entry(label='Services', content='\n\n'.join(PARAGRAPHS))
    ctx, row = context([note], brief=PARAGRAPHS[0])
    row['payload']['questions'] = []
    path = '/onboarding/knowledge/review/' + row['id']
    env = Environment(loader=FileSystemLoader(ROOT / 'src/app/templates'), autoescape=True)
    markup = env.get_template('_knowledge_review.html').render(
        review=knowledge_review.review_view(ctx, row, {'version': ctx['version']}),
        csrf_token=lambda request: 'offline', request={})
    markup = markup.replace('data-knowledge-review="', 'data-note-id="' + note['id'] + '" data-knowledge-review="', 1)
    page = ('<!doctype html><html><head><meta charset="utf-8"></head><body><main>' + markup + '</main>'
            + ''.join('<script src="/fixture/' + name + '"></script>' for name in sorted(CONTROLLERS, reverse=True))
            + '<script>' + DRIVER + '</script></body></html>').encode('utf-8')
    turns, saves, failures = [], [], []
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def reply(self, status, body, content_type='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
            self.send_response(status); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            if self.path == path: self.reply(200, page, 'text/html; charset=utf-8')
            elif self.path.startswith('/fixture/') and self.path[9:] in CONTROLLERS:
                self.reply(200, (ROOT / 'src/app/static' / self.path[9:]).read_bytes(), 'text/javascript')
            else: self.send_error(404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('content-length', 0)))
            if self.path == path + '/guide':
                body = json.loads(raw)
                try:
                    result = asyncio.run(guide.turn('offline', ctx, row, body['form'], body['question'], body['focus_hash']))
                except HTTPException as exc:
                    result = {'detail': exc.detail}; self.reply(exc.status_code, result)
                else: self.reply(200, result)
                turns.append((body, result))
            elif self.path == path:
                saves.append({'content_type': self.headers.get('content-type', ''), 'body': raw.decode('utf-8')})
                self.reply(200, b'<!doctype html><title>received</title>', 'text/html; charset=utf-8'); completed.set()
            elif self.path == '/browser-result':
                failures.append(json.loads(raw)); self.reply(200, {}); completed.set()
            else: self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.handle_error = lambda request, address: None  # keep-alive sockets reset when the browser is killed
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    command = [chromium_binary(), '--headless=new', '--no-sandbox', '--disable-gpu',
               '--disable-background-networking', '--disable-component-update', '--disable-sync',
               '--no-first-run', '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
               '--user-data-dir=' + str(tmp_path / 'profile'), f'http://127.0.0.1:{server.server_port}{path}']
    try:
        with (tmp_path / 'browser.log').open('w') as output:
            browser = subprocess.Popen(command, stdout=output, stderr=output)
            try: submitted = completed.wait(20)
            finally:
                # taskkill can stall on a busy Chromium tree; the browser process is the parent of
                # every helper, so killing it directly is the reliable fallback.
                if os.name == 'nt':
                    try: subprocess.run(['taskkill', '/PID', str(browser.pid), '/T', '/F'], stdout=output, stderr=output, timeout=5)
                    except subprocess.TimeoutExpired: pass
                browser.kill(); browser.wait(timeout=10)
    finally: server.shutdown(); server.server_close(); worker.join(timeout=5)
    assert submitted, 'Browser never submitted after %d guide turns: %s' % (len(turns), (tmp_path / 'browser.log').read_text(errors='replace')[-500:])
    assert not failures, failures
    assert row['state'] == 'pending' and ctx['brief'] == PARAGRAPHS[0]
    # The guide acknowledged exactly one overlap, from textarea.value (LF) form data.
    acknowledged = {k: v for _, result in turns for k, v in result.get('patches', {}).items() if k.startswith('match_')}
    (match_name,) = acknowledged
    assert acknowledged[match_name] == 'keep_both'
    assert all('\r' not in body['form'].get('content_' + note['id'], '') for body, _ in turns)
    # The native submission carries the same text as CRLF and the guide-ticked checkbox.
    assert saves[0]['content_type'].startswith('application/x-www-form-urlencoded')
    form = dict(parse_qsl(saves[0]['body'], keep_blank_values=True))
    assert form['operation'] == 'save' and form['content_' + note['id']] == '\r\n\r\n'.join(PARAGRAPHS)
    assert form[match_name] == 'keep_both' and form['action_' + note['id']] == 'keep'
    # The planner that guards admission accepts the browser's exact body under that key.
    notes, payload = knowledge_review.plan(ctx, row, form)
    assert len(notes) == 1 and '\r' not in notes[0]['content']
    assert payload['entries'][0]['matches'][0]['key'] == match_name.removeprefix('match_')
    assert any(body['form'].get('content_' + note['id']) == '\n\n'.join(PARAGRAPHS) for body, _ in turns)
