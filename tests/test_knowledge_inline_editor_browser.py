"""Real Chromium: one visible note, in-place editing, focus and review invalidation."""
from html import unescape
import json
from pathlib import Path
import re
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader
from app import knowledge_review
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width,theme', [(390, 'dark'), (940, 'light'), (1440, 'dark')])
def test_inline_edit_keeps_one_copy_and_other_review_decisions(tmp_path, width, theme):
    ctx, row = context([entry('one', 'Request process', 'We review requests before confirming a date. ' * 10),
                        entry('two', 'Opening hours', 'We open at nine.')])
    view = knowledge_review.review_view(ctx, row, {'action_one': 'keep', 'action_two': 'keep'})
    env = Environment(loader=FileSystemLoader(ROOT/'src/app/templates'), autoescape=True)
    body = env.get_template('_knowledge_review.html').render(review=view, request={}, csrf_token=lambda _: 'offline')
    controller = (ROOT/'src/app/static/knowledge_onboarding_review.js').read_text(encoding='utf-8')
    css = (ROOT/'src/app/static/tw.css').read_text(encoding='utf-8')
    script = r'''
    const failures=[],check=(v,m)=>{if(!v)failures.push(m)};
    const form=document.querySelector('form'),section=form.querySelector('[data-draft="one"]');
    const content=form.elements.content_one,heading=form.elements.label_one,preview=section.querySelector('[data-note-preview]');
    const tool=section.querySelector('[data-note-tool="edit"]'),bar=section.querySelector('.kr-toolbar');
    const visible=n=>n.getClientRects().length>0;
    check(visible(preview)&&!visible(content)&&!visible(heading),'initial readable note');
    const oldY=preview.getBoundingClientRect().top;
    tool.click();
    check(document.activeElement===heading,'Edit focuses heading');
    check(visible(content)&&visible(heading)&&!visible(preview),'one visible content copy');
    check(!visible(section.querySelector('[data-note-label-preview]')),'one visible title');
    check(content.getBoundingClientRect().top<bar.getBoundingClientRect().top,'editor precedes toolbar');
    check(Math.abs(content.getBoundingClientRect().top-oldY)<35,'content stays in original area');
    check(content.getBoundingClientRect().height>=100,'editor has usable height');
    heading.value='Updated subject';heading.dispatchEvent(new Event('input',{bubbles:true}));
    content.value='We confirm only after review.';content.dispatchEvent(new Event('input',{bubbles:true}));
    check(form.elements.action_one.value==='','edited note requires review');
    check(form.elements.action_two.value==='keep','other decision remains');
    check(new FormData(form).getAll('content_one').length===1,'one submitted content field');
    check(new FormData(form).getAll('label_one').length===1,'one submitted heading field');
    check(section.querySelector('[data-note-details]').open,'typing title keeps card open');
    tool.click();
    check(!visible(content)&&visible(preview)&&preview.textContent==='We confirm only after review.','Done restores edited preview');
    check(section.querySelector('[data-note-label-preview]').textContent==='Updated subject','title updates');
    check(tool.textContent==='Edit','Edit label returns');
    tool.click();check(content.value==='We confirm only after review.','reopen preserves draft');
    section.querySelector('[data-note-tool="source"]').click();
    check(!visible(content)&&visible(preview),'source view restores readable content');
    tool.click();
    check(document.documentElement.scrollWidth<=WIDTH,'no horizontal overflow');
    const result=document.createElement('pre');result.id='result';result.dataset.status=failures.length?'FAIL':'PASS';result.textContent=JSON.stringify(failures);document.body.appendChild(result);
    '''.replace('WIDTH', str(width))
    page = '<html data-theme='+json.dumps(theme)+'><head><style>'+css+'</style></head><body>'+body+'<script>'+controller+'</script><script>'+script+'</script></body></html>'
    (tmp_path/'page.html').write_text(page, encoding='utf-8')
    wrapper=tmp_path/'frame.html'
    wrapper.write_text('<style>body{margin:0}iframe{border:0}</style><iframe id="frame" width="'+str(width)+'" height="1000" src="page.html"></iframe><script>const timer=setInterval(()=>{const r=document.getElementById("frame").contentDocument.getElementById("result");if(r){document.body.appendChild(r.cloneNode(true));clearInterval(timer);}},20);</script>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--allow-file-access-from-files','--no-first-run','--disable-background-networking','--no-proxy-server','--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),'--window-size='+str(max(width,500))+',1050','--screenshot='+str(tmp_path/'inline.png'),'--virtual-time-budget=3000','--dump-dom',wrapper.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    report=re.search(r'<pre id="result"[^>]*>(.*?)</pre>',result.stdout,re.S)
    assert result.returncode==0 and report and 'data-status="PASS"' in report.group(0), unescape(report.group(0)) if report else result.stderr[-1500:]
