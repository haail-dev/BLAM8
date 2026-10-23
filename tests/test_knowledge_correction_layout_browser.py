"""Native production-CSS correction preview and source controls in both themes."""
from html import unescape
import json
from pathlib import Path
import re
import subprocess

from jinja2 import Environment, FileSystemLoader
import pytest

from app import knowledge_review, knowledge_fidelity
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('theme', ['dark','light'])
@pytest.mark.parametrize('width', [390,1280])
def test_correction_cards_apply_and_source_choices_in_native_browser(tmp_path,theme,width):
    note = entry(label='Hours and Contact',content='Our office closes at 5 pm. '+ 'Contact instructions remain available. '*7)
    ctx,row=context([note])
    row['payload']['source_snapshot']='offline'
    from app.knowledge_capture import extract
    row['payload']['source_units']=knowledge_fidelity.inventory([{'url':'https://example.test/'+'path/'*30,
        **extract('<p>Our office closes at 5 pm.</p><h2>Personal service</h2>')}])
    view=knowledge_review.review_view(ctx,row)
    view['source_review_enabled']=True
    env=Environment(loader=FileSystemLoader(ROOT/'src/app/templates'),autoescape=True)
    markup=env.get_template('_knowledge_review.html').render(review=view,csrf_token=lambda r:'offline',request={})
    css=(ROOT/'src/app/static/tw.css').read_text(encoding='utf-8')
    controller=(ROOT/'src/app/static/knowledge_source_review.js').read_text(encoding='utf-8')
    fixture={'review':{'revision':1},'summary':'Owner correction preview; nothing saved.', 'overlaps':[], 'questions':[],
        'changes':[{'id':'one','label':'Hours and Contact','before':note['content'],
                    'after':note['content'].replace('5 pm','4 pm'),'passages':[],
                    'source_choices':[{'action':'excluded','text':'A source fact explicitly excluded by the owner.','sources':['https://example.test/'],'destinations':[],'reason':'Owner requests its removal.'}],
                    'owner_replacements':[{'before':'5 pm','after':'4 pm'}]}]}
    script = r'''
    const failures=[];const check=(ok,label)=>{if(!ok)failures.push(label)};
    window.fetch=async()=>({ok:true,json:async()=>fixture});
    (async()=>{
      await window.Blam8KnowledgeSourceReview.ask('Owner confirms closing time is now 4 pm.');
      const form=document.querySelector('form'),note=document.querySelector('.kr-note');
      const button=document.querySelector('[data-source-apply]');
      check(button.tagName==='BUTTON'&&button.type==='button','semantic Apply');
      check(getComputedStyle(button).backgroundColor==='rgb(29, 79, 215)','app blue Apply');
      check(getComputedStyle(button).color==='rgb(255, 255, 255)','Apply contrast');
      button.focus();check(document.activeElement===button&&getComputedStyle(button).outlineStyle==='solid','visible keyboard focus');
      const token=getComputedStyle(document.documentElement).getPropertyValue('--c-bg-white').trim().split(/\s+/).join(', ');
      check(getComputedStyle(note).backgroundColor==='rgb('+token+')','theme card surface');
      document.querySelector('[data-note-panel=edit]').hidden=false;
      document.querySelector('[data-note-panel=source]').hidden=false;
      document.querySelector('.kr-source-coverage').open=true;
      for(const d of document.querySelectorAll('.kr-source-unit'))d.open=true;
      check(document.documentElement.scrollWidth<=innerWidth+1,'narrow document overflow');
      const source=document.querySelector('.kr-source-coverage select');source.value='unknown';
      check(new FormData(form).get(source.name)==='unknown','source decision native submission');
      check(!form.elements.guided_layout_review,'Cosmetic labels do not need mandatory approval');
      // Changing the form invalidates the preview, without an Apply request.
      button.click();check(document.querySelector('[data-source-status]').textContent.includes('changed'),'stale preview rejected');
      await window.Blam8KnowledgeSourceReview.ask('Owner confirms closing time is now 4 pm.');
      const excluded=Array.from(document.querySelectorAll('blockquote')).find(el=>el.textContent.startsWith('Exclude this source fact:'));
      check(excluded&&excluded.getBoundingClientRect().height>0,'factual exclusion is visible without expanding evidence');
      const active=document.querySelector('[data-source-apply]');
      let finish;window.fetch=()=>new Promise(resolve=>finish=resolve);
      active.click();check(active.disabled&&active.getAttribute('aria-busy')==='true','disabled loading');
      check(active.textContent.includes('Applying'),'loading label');
      finish({ok:false,json:async()=>({detail:'Synthetic failure'})});
      await new Promise(resolve=>setTimeout(resolve,0));
      check(!form.elements.content_one.disabled,'controls unlocked after failure');
      await window.Blam8KnowledgeSourceReview.ask('Owner confirms closing time is now 4 pm.').catch(()=>{});
    })().catch(e=>failures.push(String(e))).finally(()=>{
      check(document.documentElement.scrollWidth<=fixture.width+1,'final narrow overflow '+innerWidth+'/'+document.documentElement.scrollWidth);
      const result=document.getElementById('native-result');result.dataset.status=failures.length?'FAIL':'PASS';result.textContent=JSON.stringify(failures);
    });
    '''
    # The final re-preview uses an immediate transport after testing failure.
    script=script.replace("await window.Blam8KnowledgeSourceReview.ask('Owner confirms closing time is now 4 pm.').catch(()=>{});",
        "window.fetch=async()=>({ok:true,json:async()=>fixture}); await window.Blam8KnowledgeSourceReview.ask('Owner confirms closing time is now 4 pm.');")
    fixture['width']=width
    page=tmp_path/'correction.html'
    page.write_text('<!doctype html><html data-theme="'+theme+'"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
        '<style>'+css+'</style></head><body class="bg-slate-100 text-slate-700" style="margin:0;padding:16px">'
        '<main style="max-width:720px;margin:auto">'+markup+'</main><pre id="native-result">PENDING</pre>'
        '<script>const fixture='+json.dumps(fixture)+';</script><script>'+controller+'</script><script>'+script+'</script></body></html>',encoding='utf-8')
    # Chromium's desktop window has a 500px minimum. An exact-width native
    # frame exercises the requested viewport instead of accepting that minimum.
    wrapper=tmp_path/'viewport.html'
    wrapper.write_text('<!doctype html><style>html,body{margin:0}iframe{border:0;display:block}</style>'
        '<iframe id="viewport" width="'+str(width)+'" height="1100" src="correction.html"></iframe>'
        '<script>const timer=setInterval(()=>{const frame=document.getElementById("viewport");'
        'const report=frame.contentDocument&&frame.contentDocument.getElementById("native-result");'
        'if(report&&report.dataset.status){document.body.appendChild(report.cloneNode(true));clearInterval(timer);}},20);</script>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--no-first-run','--allow-file-access-from-files',
        '--disable-background-networking','--disable-component-update','--disable-sync','--no-proxy-server',
        '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),
        '--window-size='+str(width)+',1100','--force-device-scale-factor=1',
        '--screenshot='+str(tmp_path/(theme+'-'+str(width)+'.png')),
        '--virtual-time-budget=5000','--dump-dom',wrapper.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    report=re.search(r'<pre id="native-result"[^>]*>(.*?)</pre>',result.stdout,re.S)
    assert result.returncode==0 and report and 'data-status="PASS"' in report.group(0), unescape(report.group(0)) if report else result.stderr[-2000:]
