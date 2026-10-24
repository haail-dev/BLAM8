"""Saved Knowledge stays clear at phone and desktop widths, without exposing IDs."""
from html import unescape
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from app import onboarding
from tests.test_knowledge_guidance_browser import chromium_binary
ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('width,theme', [(390,'dark'), (940,'light')])
def test_saved_knowledge_layout(tmp_path, width, theme):
    css=(ROOT/'src/app/static/tw.css').read_text(encoding='utf-8')
    env=Environment(loader=ChoiceLoader([DictLoader({'_theme_head.html':'<style>'+css+'</style>'}),FileSystemLoader(ROOT/'src/app/templates')]),autoescape=True)
    source='Opening hours · technical123'
    page=env.get_template('onboarding/knowledge.html').render(
        **onboarding._step_ctx('knowledge'),request=SimpleNamespace(state=SimpleNamespace(user_email='owner@example.test',tenant_id='offline'),url=SimpleNamespace(path='/onboarding/knowledge')),
        configured=True,asset_rev='offline',csrf_token=lambda _: 'offline',device_sync_enabled=lambda:False,
        onboarding_identity=lambda _: {'mailbox':'owner@example.test','same':True},pending_reviews=[],saved_review_ids=['saved'],
        sources=[{'source':source,'display_label':'Opening hours','chunks':1}],source_texts={source:'We close at four.'},
        site_url='https://example.test/',site_done=True,site_state={'state':'saved','pending_notes':0,'saved_notes':1},next_label='Next: Guided setup')
    page=re.sub(r'<script\b[^>]*\bsrc=[^>]*></script>','',page)
    page=page.replace('<head>','<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">')
    script='''
    document.documentElement.setAttribute('data-theme',THEME);
    const failures=[], check=(v,m)=>{if(!v)failures.push(m)};
    check(document.documentElement.scrollWidth<=WIDTH,'horizontal overflow');
    const status=document.querySelector('[data-knowledge-saved-status]');
    check(status.textContent.includes('1 note saved. No drafts waiting'),'saved status');
    check(document.getElementById('site-lifecycle').hidden,'one completion summary');
    const siteDone=document.getElementById('site-done');
    check(siteDone.textContent.includes('Done')&&!siteDone.querySelector('button,a'),'website completion has no reread action');
    check(!document.body.innerText.includes('Read again'),'saved page does not offer rereading');
    check(document.querySelector('[data-onboarding-heading]').textContent==='Your Knowledge','saved heading');
    const note=document.querySelector('[data-kb-note]'), summary=note.querySelector('summary');
    check(summary.textContent.includes('Opening hours')&&!summary.textContent.includes('technical123'),'title has no identifier');
    note.open=true;
    const evidence=note.querySelector('details');
    check(!evidence.open&&evidence.textContent.includes('technical123'),'technical evidence retained but collapsed');
    check(document.documentElement.scrollWidth<=WIDTH,'expanded note overflows');
    const result=document.createElement('pre');result.id='result';result.dataset.status=failures.length?'FAIL':'PASS';result.textContent=JSON.stringify(failures);document.body.appendChild(result);
    '''.replace('THEME',repr(theme)).replace('WIDTH',str(width))
    (tmp_path/'page.html').write_text(page.replace('</body>','<script>'+script+'</script></body>'),encoding='utf-8')
    wrapper=tmp_path/'frame.html';wrapper.write_text('<style>body{margin:0}iframe{border:0}</style><iframe id="frame" width="'+str(width)+'" height="1000" src="page.html"></iframe><script>const timer=setInterval(()=>{const r=document.getElementById("frame").contentDocument.getElementById("result");if(r){document.body.appendChild(r.cloneNode(true));clearInterval(timer);}},20);</script>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--allow-file-access-from-files','--no-first-run','--disable-background-networking','--no-proxy-server','--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),'--window-size='+str(max(width,500))+',1050','--force-device-scale-factor=1','--screenshot='+str(tmp_path/'saved.png'),'--virtual-time-budget=3000','--dump-dom',wrapper.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    report=re.search(r'<pre id="result"[^>]*>(.*?)</pre>',result.stdout,re.S)
    assert result.returncode==0 and report and 'data-status="PASS"' in report.group(0), unescape(report.group(0)) if report else result.stderr[-1200:]
