"""The setup walk as numbered progress in the app shell.

Renders the real app/base.html. The markup tests need no browser; the geometry test
measures the production CSS and script in an isolated, offline Chromium profile, the same
way test_onboarding_layout_browser.py does (build the CSS first: npm run build:css).
"""
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest
from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app import setup_handoff
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/app/static'
CHILD = '{% extends "app/base.html" %}{% block content %}<div class="p-6">Diary settings</div>{% endblock %}'


def step_list(done=()):
    rows = [{'key': 'diary', 'label': 'Diary & scheduling settings', 'href': '/app/diary/settings'},
            {'key': 'requirements', 'label': 'Booking requirements', 'href': '/app/diary/intake'},
            {'key': 'dependants', 'label': 'Dependants', 'href': '/app/diary/dependants'},
            {'key': 'crm', 'label': 'CRM settings', 'href': '/app/crm/settings', 'move_on': 'Set a pipeline to move on'},
            {'key': 'invoicing', 'label': 'Getting paid', 'href': '/app/invoices/settings'}]
    return [dict(row, done=row['key'] in done) for row in rows]


def render_shell(path='/app/diary/settings', done=(), rows=None, inline=False):
    """The real shell around a stub page. inline=True embeds the production CSS and the
    stepper script so an offline browser can run them."""
    css = (STATIC / 'tw.css').read_text(encoding='utf-8') if inline else ''
    env = Environment(loader=ChoiceLoader([DictLoader({'_theme_head.html': '<style>' + css + '</style>', 'page.html': CHILD}),
                                           FileSystemLoader(ROOT / 'src/app/templates')]),
                      autoescape=True, undefined=ChainableUndefined)
    onboarded = datetime.now(timezone.utc)
    request = SimpleNamespace(url=SimpleNamespace(path=path), cookies={}, query_params={},
                              state=SimpleNamespace(user_email='owner@example.test', tenant_id='offline', viewing_as=None))
    html = env.get_template('page.html').render(
        request=request, asset_rev='offline', csrf_token=lambda r: 'offline', concierge_on=True, active='diary_settings',
        device_sync_enabled=lambda: False, concierge_authed=True,
        setup_strip_raw={'steps': rows or step_list(done), 'onboarded_at': onboarded, 'dismissed_at': None},
        setup_strip_fn=setup_handoff.strip)
    if inline:
        for name, pattern, wrap in (('setup_stepper.css', r'<link rel="stylesheet" href="/static/setup_stepper\.css[^"]*">', '<style>%s</style>'),
                                    ('setup_stepper.js', r'<script src="/static/setup_stepper\.js[^"]*" defer></script>', '<script>%s</script>')):
            body = (STATIC / name).read_text(encoding='utf-8')
            html = re.sub(pattern, lambda m: wrap % body, html)
        html = re.sub(r'<script\b[^>]*\bsrc=[^>]*></script>', '', html)
        html = html.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                            'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; img-src data:">', 1)
    return html


class Strip(HTMLParser):
    """What the strip draws: a circle per step and the name under the current one."""
    def __init__(self, html):
        super().__init__()
        self.items, self.dots, self.labels = [], [], []
        self._dot = self._label = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a, cls = dict(attrs), dict(attrs).get('class') or ''
        if tag == 'li' and 'setup-stepper-item' in cls: self.items.append(a)
        if tag == 'span' and 'setup-stepper-dot' in cls: self._dot = True; self.dots.append('')
        if tag == 'span' and 'setup-stepper-label' in cls: self._label = True; self.labels.append('')

    def handle_endtag(self, tag):
        if tag == 'span': self._dot = self._label = False

    def handle_data(self, data):
        if self._dot: self.dots[-1] += data
        if self._label: self.labels[-1] += data


def test_the_first_step_shows_five_circles_and_its_name_without_settings():
    html = render_shell()
    page = Strip(html)
    assert [d.strip() for d in page.dots] == ['5', '6', '7', '8', '9']
    assert [i.get('aria-current') for i in page.items] == ['step', None, None, None, None]
    assert page.labels == ['Diary & scheduling']
    assert 'setup-stepper-track' in html
    # The screen-reader summary keeps what the old sentence said, in the same short name.
    assert 'Setup · 4 of 9 done. Step 5 of 9: Diary &amp; scheduling<' in html


def test_a_saved_step_shows_a_tick_and_stays_the_current_name():
    page = Strip(render_shell(done=('diary',)))
    assert [d.strip() for d in page.dots] == ['✓', '6', '7', '8', '9']
    assert page.labels == ['Diary & scheduling']          # still the page they are standing on


def test_off_a_step_page_the_next_open_step_is_the_focus():
    page = Strip(render_shell(path='/app/', done=('diary',)))
    assert page.labels == ['Booking requirements']
    assert [i.get('aria-current') for i in page.items] == [None, 'step', None, None, None]


def test_no_name_in_the_progress_bar_ends_in_settings():
    page = Strip(render_shell(path='/app/crm/settings', done=('diary', 'requirements', 'dependants')))
    assert page.labels == ['CRM']
    shown = page.labels + [i.get('title', '') for i in page.items]
    assert shown == ['CRM', 'Diary & scheduling', 'Booking requirements', 'Dependants', 'CRM', 'Getting paid']


def test_a_step_badge_overrides_the_circle_text():
    rows = step_list()
    rows[1]['badge'] = '5A'
    assert [d.strip() for d in Strip(render_shell(rows=rows)).dots][:3] == ['5', '5A', '7']


@pytest.mark.parametrize('path, done', [
    ('/app/diary/settings', ()),                                                    # on the first step
    ('/app/diary/intake', ('diary',)),                                              # mid-walk
    ('/app/', ('diary', 'requirements', 'dependants')),                             # not on a step page
    ('/app/invoices/settings', ('diary', 'requirements', 'dependants', 'crm')),     # on the last step
])
def test_the_strip_is_the_circles_and_a_name_and_nothing_to_click(path, done):
    html = render_shell(path=path, done=done)
    start = re.search(r'<div[^>]*data-setup-strip', html).start()   # the element, not the CSS rule that names it
    strip = html[start:html.index('setup_stepper.js', start)]
    # The walk has no skipping (Kris 2026-10-22): no links, no buttons or forms, no hint, no Hide.
    for forbidden in ('<a ', '<form', '<button', 'data-setup-next', 'Save this page to move on', 'Hide', 'Next'):
        assert forbidden not in strip, forbidden
    assert len(Strip(html).items) == 5               # steps 1-4 are behind the owner
    assert 'Step 5 of 9 ·' not in html               # the old sentence is gone


@pytest.mark.parametrize('width', [320, 390, 800, 1280])
@pytest.mark.parametrize('scenario', ['first-step', 'middle-step', 'last-step'])
def test_the_strip_is_one_compact_row_clear_of_the_assistant(tmp_path, width, scenario):
    # last-step puts the name under the final circle, where it has to clamp at the right edge.
    options = {'first-step': {}, 'middle-step': {'path': '/app/diary/dependants', 'done': ('diary', 'requirements')},
               'last-step': {'path': '/app/invoices/settings', 'done': ('diary', 'requirements', 'dependants', 'crm')}}
    page = render_shell(inline=True, **options[scenario])
    assertions = r'''
    const failures=[];
    const check=(ok,message)=>{if(!ok) failures.push(message)};
    const rect=el=>el.getBoundingClientRect();
    const strip=document.querySelector('[data-setup-strip]'), stepper=strip.querySelector('[data-setup-stepper]');
    const panel=document.getElementById('bc-panel');
    const desktop=innerWidth>=768;
    if(desktop){panel.classList.remove('hidden');panel.classList.add('flex');}
    window.dispatchEvent(new Event('resize'));      // the script re-measures; the strip's width changed
    const s=rect(strip), st=rect(stepper), label=stepper.querySelector('[data-setup-stepper-label]'), lr=rect(label);
    const dots=Array.from(stepper.querySelectorAll('.setup-stepper-dot'));
    check(dots.length===5,'five circles');
    dots.forEach(d=>{const r=rect(d);check(r.left>=st.left-1&&r.right<=st.right+1&&r.width>0,'circle inside the stepper')});
    check(lr.left>=st.left-1&&lr.right<=st.right+1,'name inside the stepper');
    const vars=getComputedStyle(stepper);
    check(Math.abs(parseFloat(vars.getPropertyValue('--label-start'))-(lr.left-st.left))<1,'blue track stops at the name');
    check(Math.abs(parseFloat(vars.getPropertyValue('--label-end'))-(lr.right-st.left))<1,'grey track resumes after the name');
    const track=rect(stepper.querySelector('.setup-stepper-track'));
    check(Math.abs(track.top+track.height/2-(lr.top+lr.height/2))<=1.5,'track runs through the name');
    check(document.documentElement.scrollWidth<=innerWidth+1,'no horizontal overflow');
    if(desktop){
      // One row, and the assistant (fixed 80px from the top) is not run into or covered.
      check(s.height<=44,'strip is one row: '+s.height);
      const dock=rect(panel);
      check(dock.top>=s.bottom-0.5,'strip ends above the dock: '+s.bottom+' vs '+dock.top);
      Array.from(strip.firstElementChild.children).forEach(c=>{if(rect(c).width>0) check(rect(c).right<=dock.left+1,'content stays left of the open assistant')});
    } else check(s.height<=70,'phone strip is at most two rows: '+s.height);
    document.getElementById('layout-result').textContent=JSON.stringify({failures,width:innerWidth,height:s.height});
    document.getElementById('layout-result').dataset.status=failures.length?'FAIL':'PASS';
    '''
    page = page.replace('</body>', '<pre id="layout-result"></pre><script>' + assertions + '</script></body>')
    (tmp_path / 'content.html').write_text(page, encoding='utf-8')
    fixture = tmp_path / 'layout.html'
    # Chrome's Windows window has a 500px minimum: an exact-width iframe exercises phone widths.
    fixture.write_text('<!doctype html><style>body{margin:0}iframe{border:0;display:block}</style>'
                       + f'<iframe width="{width}" height="900" src="content.html" onload="'
                       + "document.body.appendChild(this.contentDocument.getElementById('layout-result').cloneNode(true))"
                       + '"></iframe>', encoding='utf-8')
    command = [chromium_binary(), '--headless=new', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files',
               '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-default-apps',
               '--no-first-run', '--no-default-browser-check', '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND',
               '--user-data-dir=' + str(tmp_path / 'profile'), '--virtual-time-budget=2000', f'--window-size={width},900',
               '--dump-dom', fixture.as_uri()]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    report = re.search(r'<pre id="layout-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
    assert report, result.stdout[-2000:]
    measured = json.loads(unescape(report.group(1)))
    assert measured['width'] == width, measured
    assert not measured['failures'], measured
