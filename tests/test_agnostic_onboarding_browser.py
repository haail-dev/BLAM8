"""Native offline layout/keyboard proof for the real grouped template and shell."""
from html import unescape
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest
from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader
from app import onboarding_contract as contract, onboarding_routes, setup_handoff
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]


def render_group(theme, *, long=False):
    state = contract.neutral_state()
    state['capabilities'] = {k: 'needed' for k in contract.CAPABILITIES}
    state['position'] = 'diary.activities'
    if long:
        state['labels']['diary.activities'] = {'text': 'Activities, permissions and confirmed arrangements for several different purposes'}
    context = dict(state=state, cfg={}, config={}, modules={'scheduling', 'crm'}, groups=[],
                   invoice={}, knowledge=None, knowledge_unavailable=False)
    groups = contract.group_steps(context['modules'], state=state)
    css = (ROOT / 'src/app/static/tw.css').read_text(encoding='utf-8')
    env = Environment(loader=ChoiceLoader([DictLoader({'_theme_head.html': '<style>' + css + '</style>'}),
                       FileSystemLoader(ROOT / 'src/app/templates')]), autoescape=True, undefined=ChainableUndefined)
    request = SimpleNamespace(url=SimpleNamespace(path='/app/setup/diary'), cookies={}, query_params={},
        state=SimpleNamespace(user_email='owner@example.test', tenant_id='offline', viewing_as=None))
    page = env.get_template('app/onboarding_group.html').render(request=request, asset_rev='offline',
        csrf_token=lambda r: 'offline', concierge_on=False, active='settings',
        device_sync_enabled=lambda: False, review_groups=groups,
        onboarding_app_progress=setup_handoff.onboarding_progress,
        setup_strip_raw={'steps': groups, 'onboarded_at': '2026-10-23', 'dismissed_at': None},
        setup_strip_fn=setup_handoff.strip, **onboarding_routes.page_context(context, groups[0]))
    for name in ('onboarding_group.css', 'setup_stepper.css', 'guided_setup.css'):
        body = (ROOT / 'src/app/static' / name).read_text(encoding='utf-8')
        page = re.sub(r'<link rel="stylesheet" href="/static/' + re.escape(name) + r'[^\"]*">', lambda m: '<style>' + body + '</style>', page)
    body = (ROOT / 'src/app/static/setup_stepper.js').read_text(encoding='utf-8')
    page = re.sub(r'<script src="/static/setup_stepper\.js[^\"]*" defer></script>', lambda m: '<script>' + body + '</script>', page)
    dirty = (ROOT / 'src/app/static/dirty_save.js').read_text(encoding='utf-8')
    page = re.sub(r'<script src="/static/dirty_save\.js[^\"]*" defer></script>', lambda m: '<script>' + dirty + '</script>', page)
    page = re.sub(r'<script\b[^>]*\bsrc=[^>]*></script>', '', page)
    page = page.replace('<html lang="en"', '<html data-theme="' + theme + '" lang="en"')
    return page.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; img-src data:">')


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('long', [False, True], ids=['primary', 'long-labels'])
def test_group_disclosures_focus_long_labels_and_geometry(tmp_path, theme, width, long):
    page = render_group(theme, long=long)
    assertions = r'''
    const failures=[];const check=(ok,message)=>{if(!ok)failures.push(message)};
    check(document.documentElement.scrollWidth<=innerWidth+1,'horizontal overflow');
    check(document.querySelectorAll('[data-step-name-toggle]').length===7,'seven onboarding steps');
    check(!!document.querySelector('[data-onboarding-brand]')&&!document.querySelector('[data-setup-strip]'),'shared onboarding header');
    const group=document.querySelector('[data-onboarding-group]');
    check(group && group.querySelectorAll('[data-review-substep]').length===3,'three sections');
    const section=group.querySelector('[data-review-substep]');
    const summary=section.querySelector('summary');
    summary.focus();check(document.activeElement===summary,'keyboard focus reaches summary');
    check(getComputedStyle(summary).outlineStyle!=='none','focus indicator visible');
    const before=section.open;summary.click();check(section.open!==before,'native disclosure toggles');
    section.open=true;
    const q=section.querySelector('[data-question-id]');q.open=true;
    check(q.querySelector('form textarea') || q.querySelector('form select'),'native answer control');
    const field=q.querySelector('textarea,select');field.focus();check(document.activeElement===field,'answer focus');
    const ids=[...group.querySelectorAll('[id]')].map(e=>e.id);check(new Set(ids).size===ids.length,'unique ids');
    const forms=[...group.querySelectorAll('form')];
    check(forms.every(f=>f.method==='post' && f.querySelector('[name=csrf_token]') && f.querySelector('[name=revision]')),'csrf and revision on each save');
    const controls=[...group.querySelectorAll('input:not([type=hidden]),select,textarea,button')].filter(e=>e.getBoundingClientRect().width>0);
    controls.forEach(e=>{const r=e.getBoundingClientRect();check(r.left>=-1 && r.right<=innerWidth+1,'control outside viewport')});
    const text=group.innerText;check(text.includes('Activities'),'section label readable');
    const review=section.querySelector('input[name=operation][value=review]')?.closest('form');
    if(review){const save=review.querySelector('[data-save-row]');check(save?.hidden,'unchanged section save hidden');
      const choice=review.querySelector('[name=state]'),old=choice.value;choice.value='reviewed';review.dispatchEvent(new Event('change',{bubbles:true}));check(!save.hidden,'changed section save appears');
      choice.value=old;review.dispatchEvent(new Event('change',{bubbles:true}));check(save.hidden,'undo hides section save again');}
    check(!text.includes('Answer state')&&!text.includes('Scope and saved activities')&&!text.includes('Add activity scope')&&!text.includes('operational readiness'),'plain primary wording');
    check(!group.querySelector('[data-question-id="response.urgent"]'),'no unselected urgent-response question');
    check(!group.querySelector('[data-question-id="service.price"]'),'activity question not leaked into organisation scope');
    document.getElementById('layout-result').textContent=JSON.stringify({failures,width:innerWidth,theme:document.documentElement.dataset.theme,form_count:forms.length,open_question_count:group.querySelectorAll('[data-question-id][open]').length});
    '''
    page = page.replace('</body>', '<pre id="layout-result"></pre><script>document.addEventListener("DOMContentLoaded",()=>{' + assertions + '},{once:true});</script></body>')
    (tmp_path / 'content.html').write_text(page, encoding='utf-8')
    fixture = tmp_path / 'layout.html'
    fixture.write_text('<!doctype html><style>body{margin:0}iframe{border:0;display:block}</style>'
        + f'<iframe width="{width}" height="1050" src="content.html" onload="'
        + "document.body.appendChild(this.contentDocument.getElementById('layout-result').cloneNode(true))"
        + '"></iframe>', encoding='utf-8')
    screenshot = tmp_path / (theme + '-' + str(width) + ('-long' if long else '-primary') + '.png')
    command = [chromium_binary(), '--headless=new', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files',
        '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-default-apps',
        '--no-first-run', '--no-default-browser-check', '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--user-data-dir=' + str(tmp_path / 'profile'), '--virtual-time-budget=2000', f'--window-size={max(width,600)},1100',
        '--screenshot=' + str(screenshot), '--dump-dom', fixture.as_uri()]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    report = re.search(r'<pre id="layout-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
    assert report, result.stdout[-2000:]
    measured = json.loads(unescape(report.group(1)))
    assert measured['width'] == width and measured['theme'] == theme, measured
    assert not measured['failures'], measured
    (tmp_path / 'measurements.json').write_text(json.dumps(measured, indent=2), encoding='utf-8')
