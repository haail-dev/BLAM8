"""The shared wizard stepper discloses names without navigating or progressing."""
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from app import onboarding

ROOT=Path(__file__).resolve().parents[1]


def render_stepper(step='knowledge', **extra):
    env=Environment(loader=ChoiceLoader([DictLoader({'_theme_head.html':'','_concierge.html':''}),
        FileSystemLoader(ROOT/'src/app/templates')]),autoescape=True)
    request=SimpleNamespace(state=SimpleNamespace(user_email='owner@example.test'),url=SimpleNamespace(path='/onboarding/'+step))
    return env.get_template('onboarding/base.html').render(request=request,asset_rev='offline',
        **{**onboarding._step_ctx(step), **extra})


class Tags(HTMLParser):
    def __init__(self,html):
        super().__init__();self.tags=[];self.feed(html)
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))


@pytest.mark.parametrize('step',['interview','mailbox','voice','knowledge','industry','data'])
def test_shared_stepper_has_one_visible_current_name_and_safe_disclosures(step):
    html=render_stepper(step);tags=Tags(html).tags
    buttons=[attrs for tag,attrs in tags if 'data-step-name-toggle' in attrs]
    labels=[attrs for tag,attrs in tags if attrs.get('class')=='onboarding-step-label']
    assert len(buttons)==len(labels)==7
    assert all(button['type']=='button' and 'href' not in button and 'onclick' not in button for button in buttons)
    active=[button for button in buttons if button.get('aria-current')=='step']
    assert len(active)==1 and active[0]['aria-expanded']=='true'
    visible=[label for label in labels if 'hidden' not in label]
    assert len(visible)==1 and visible[0]['id']==active[0]['aria-controls']
    assert all(button['aria-controls'] in {label['id'] for label in labels} for button in buttons)
    assert 'Step 7: Charges and payments, upcoming' in html
    assert 'data-step-line' not in html
    assert 'inside the app)' not in html
    assert 'class="onboarding-progress"' in html
    assert "--label-start" in html and "--label-end" in html


def test_stepper_has_no_band_and_the_current_name_is_the_bar_blue():
    html=render_stepper('knowledge')
    sticky=html.split('body[data-onboarding-shell] [data-stepper-total] {',1)[1].split('}',1)[0]
    assert 'position:sticky' in sticky and 'background' not in sticky and 'box-shadow' not in sticky
    bar=html.split('.onboarding-progress {',1)[1].split('}',1)[0]
    assert 'linear-gradient(to right,#2450e6 0' in bar
    name=html.split('.onboarding-step-toggle[data-current-step] + .onboarding-step-label {',1)[1].split('}',1)[0]
    assert name.strip()=='color:#2450e6;'


def test_stepper_disclosure_controller_click_keyboard_and_reset_replay():
    result=subprocess.run([shutil.which('node'),'tests/replays/onboarding_stepper.mjs'],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
