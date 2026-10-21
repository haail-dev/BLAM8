"""Measure real onboarding geometry in isolated, offline Chromium profiles."""
from html import unescape
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app import onboarding, knowledge_review
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width', [320, 390, 768, 820, 940, 1440])
@pytest.mark.parametrize('long_labels', [False, True])
def test_onboarding_progress_and_knowledge_layout(tmp_path, width, long_labels):
    css = (ROOT / 'src/app/static/tw.css').read_text(encoding='utf-8')
    env = Environment(loader=ChoiceLoader([
        DictLoader({'_theme_head.html': '<style>' + css + '</style>'}),
        FileSystemLoader(ROOT / 'src/app/templates')]), autoescape=True)
    ctx = onboarding._step_ctx('knowledge')
    if long_labels:
        ctx['tuning'] = [{**item, 'label': 'An exceptionally long setup label ' * 8}
                         for item in ctx['tuning']]
    request = SimpleNamespace(state=SimpleNamespace(
        user_email='long.account.identity@example.test', tenant_id='offline'),
        url=SimpleNamespace(path='/onboarding/knowledge'))
    review_context, review_row = context([entry()])
    review = knowledge_review.review_view(review_context, review_row, {'version':'version'})
    page = env.get_template('onboarding/knowledge.html').render(
        **ctx, request=request, configured=True, asset_rev='offline',
        csrf_token=lambda request: 'offline', device_sync_enabled=lambda: False,
        pending_reviews=[review],
        onboarding_identity=lambda request: {'mailbox':'another-long-mailbox@example.test','same':False},
        site_url='https://example.test/' + 'long/' * 30,
        next_label='Next: Diary and scheduling settings',
        site_state={'state':'pending', 'pending_notes':9})
    if long_labels:
        # An end-position active label exercises clamping independently of the
        # normal wizard's four current steps and future tuning disclosures.
        page = page.replace('</main>', '</main><script>' + """
          const buttons=Array.from(document.querySelectorAll('[data-step-name-toggle]'));
          buttons.forEach(button=>{button.removeAttribute('data-current-step');button.removeAttribute('aria-current');
            button.setAttribute('aria-expanded','false');document.getElementById(button.getAttribute('aria-controls')).hidden=true;});
          const end=buttons[buttons.length-1];end.setAttribute('data-current-step','');end.setAttribute('aria-current','step');
          end.setAttribute('aria-expanded','true');
          const name=document.getElementById(end.getAttribute('aria-controls'));name.hidden=false;name.removeAttribute('data-inspected-label');
        """ + '</script>')
    # Keep real inline controllers/styles, prohibit external scripts, fonts and fetches.
    page = re.sub(r'<script\b[^>]*\bsrc=[^>]*></script>', '', page)
    page = page.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">')
    assertions = r'''
    const failures=[];
    const check=(ok,message)=>{if(!ok) failures.push(message)};
    const rect=el=>el.getBoundingClientRect();
    const stepper=document.querySelector('[data-stepper-total]');
    function verifyLabel(button) {
      const label=document.getElementById(button.getAttribute('aria-controls'));
      const r=rect(label),s=rect(stepper);
      check(r.left>=s.left-1 && r.right<=s.right+1,'label outside progress strip');
    }
    const current=stepper.querySelector('[data-current-step]');
    verifyLabel(current);
    const label=rect(document.getElementById(current.getAttribute('aria-controls')));
    const s=rect(stepper), style=getComputedStyle(stepper);
    check(Math.abs(parseFloat(style.getPropertyValue('--label-start'))-(label.left-s.left))<1,'blue stop');
    check(Math.abs(parseFloat(style.getPropertyValue('--label-end'))-(label.right-s.left))<1,'gray restart');
    const track=rect(stepper.querySelector('.onboarding-progress'));
    check(Math.abs(track.top+track.height/2-(label.top+label.height/2))<=1,'track label vertical alignment');
    for(const button of stepper.querySelectorAll('button')) {
      button.click();verifyLabel(button);
      button.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));
      check(button===current || button.getAttribute('aria-expanded')==='false','escape disclosure');
    }
    const email=rect(document.querySelector('[data-signed-in-as]'));
    const logout=rect(document.querySelector('a[href="/logout"]'));
    check(logout.left-email.right<=13 && logout.left>=email.right,'identity beside logout');
    check(logout.right<=innerWidth && email.left>=0,'identity within viewport');
    check(document.getElementById('bc-close'),'close retained');
    const actions=Array.from(document.querySelectorAll('.kr-batch-actions button'));
    if(rect(document.querySelector('.knowledge-review')).width<=576) check(actions.every(button=>Math.abs(rect(button).width-rect(actions[0]).width)<1),'uniform review actions');
    check(!document.getElementById('bc-clear'),'onboarding reset hidden');
    // The card starts just under the progress strip (was ~18px; stays compact but never touching).
    const gap=rect(document.querySelector('[data-onboarding-column] > .bg-white')).top-rect(stepper.querySelector('.onboarding-progress')).bottom;
    check(gap>=6 && gap<=14,'progress-to-card gap '+gap);
    // The scrolling body fades into the page colour instead of ending in a hard line.
    // getComputedStyle objects are live: keep the light colour as a string before switching theme.
    const lightBackground=getComputedStyle(document.body).backgroundColor, fade=getComputedStyle(document.body,'::after');
    check(fade.content!=='none' && fade.position==='fixed' && fade.pointerEvents==='none','bottom fade is a fixed click-through overlay');
    check(fade.bottom==='0px' && parseFloat(fade.height)>=32,'bottom fade covers the viewport edge');
    check(parseFloat(fade.height)<=parseFloat(getComputedStyle(document.querySelector('[data-onboarding-main]')).paddingBottom)+0.5,'bottom fade must not tint the last content');
    check(Number(fade.zIndex)<Number(getComputedStyle(document.getElementById('blam8-concierge')).zIndex),'bottom fade stays under the assistant dock');
    check(fade.backgroundImage.includes(lightBackground),'bottom fade ends in the page colour');
    document.documentElement.setAttribute('data-theme','dark');
    const darkBackground=getComputedStyle(document.body).backgroundColor;
    check(darkBackground!==lightBackground && getComputedStyle(document.body,'::after').backgroundImage.includes(darkBackground),'bottom fade follows the dark theme');
    document.documentElement.removeAttribute('data-theme');
    const cta=document.getElementById('bc-cta');
    cta.classList.remove('hidden');cta.innerHTML='<button class="w-full rounded-lg bg-brand text-white text-sm font-semibold" style="display:block;height:36px;width:100%">Continue</button><p class="text-xs text-slate-500 text-center">Or check the form over first.</p>';
    const panel=document.getElementById('bc-panel'); panel.classList.remove('hidden');panel.classList.add('flex');
    const cr=rect(cta), first=rect(cta.firstElementChild), last=rect(cta.lastElementChild);
    check(Math.abs((first.top-cr.top)-(cr.bottom-last.bottom))<=2,'balanced CTA insets');
    for(const selector of ['[name=operation]', '#site-box','#site-url','#site-go','[data-onb-continue]','[data-knowledge-navigation]']) {
      const r=rect(document.querySelector(selector));check(r.left>=0 && r.right<=innerWidth+1,selector+' outside viewport');
    }
    panel.classList.add('hidden');
    document.querySelector('[data-knowledge-later]').hidden=false;
    document.activeElement.blur();
    document.getElementById('layout-result').textContent=JSON.stringify({failures,width:innerWidth});
    document.getElementById('layout-result').dataset.status=failures.length?'FAIL':'PASS';
    '''
    page = page.replace('</body>', '<pre id="layout-result"></pre><script>' + assertions + '</script></body>')
    fixture = tmp_path / 'layout.html'
    # Chrome's Windows window has a 500px minimum. An exact-width browsing
    # context exercises mobile media queries without depending on that limit.
    frame = tmp_path / 'content.html'
    frame.write_text(page, encoding='utf-8')
    fixture.write_text('<!doctype html><style>body{margin:0}iframe{border:0;display:block}</style>'
        + f'<iframe width="{width}" height="1000" src="content.html" onload="'
        + "document.body.appendChild(this.contentDocument.getElementById('layout-result').cloneNode(true))"
        + '"></iframe>', encoding='utf-8')
    command = [chromium_binary(), '--headless=new', '--no-sandbox', '--disable-gpu',
        '--allow-file-access-from-files', '--disable-background-networking', '--disable-component-update', '--disable-sync',
        '--disable-default-apps', '--no-first-run', '--no-default-browser-check',
        '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--user-data-dir=' + str(tmp_path / 'profile'), '--virtual-time-budget=2000',
        f'--window-size={width},1000', '--dump-dom', fixture.as_uri()]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    report = re.search(r'<pre id="layout-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
    assert report, result.stdout[-2000:]
    measured = json.loads(unescape(report.group(1)))
    assert measured['width'] == width, measured
    assert not measured['failures'], measured
