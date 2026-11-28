"""The real app base continues the Knowledge shell only on setup surfaces."""
from types import SimpleNamespace
import pytest
from jinja2 import ChoiceLoader, DictLoader, ChainableUndefined
from app import onboarding_contract, setup_handoff
from app.templating import templates
from tests.test_onboarding_stepper import Tags


def render(path, done=(), status=None, dismissed=False, template='shell-test.html', **context):
    env=templates.env.overlay(undefined=ChainableUndefined,loader=ChoiceLoader([
        DictLoader({'shell-test.html':'{% extends "app/base.html" %}{% block heading %}Native editor{% endblock %}{% block content %}<form data-native-form><input name="value"></form>{% endblock %}',
                    '_theme_head.html':''}),templates.env.loader]))
    groups=onboarding_contract.group_steps({'scheduling','crm'})
    for group in groups:group['done']=group['key'] in done
    request=SimpleNamespace(url=SimpleNamespace(path=path),state=SimpleNamespace(user_email='owner@example.test',tenant_id='offline'),cookies={},query_params={})
    return env.get_template(template).render(request=request,asset_rev='test',**context,
        onboarding_identity=lambda _:dict(mailbox='owner@example.test',same=True),
        device_sync_enabled=lambda:False,csrf_token=lambda _:'test',
        guided_setup_meta=dict(status=status) if status else None,
        setup_strip_raw=dict(steps=groups,dismissed_at='dismissed' if dismissed else None),
        setup_strip_fn=setup_handoff.strip,setup_prefill={'page':'test'})


@pytest.mark.parametrize('fleet', [False, True])
def test_diary_settings_help_stays_with_heading_during_setup(fleet):
    html = render('/app/diary/settings', done=('records',), template='app/diary_settings.html',
                  fleet_mode=fleet, scheduling_cfg={}, day_hours={}, booking_mode_snapshot={})
    header = html.split('<h1 data-onboarding-heading', 1)[1].split('</div>', 1)[0]
    assert '>Diary settings</h1>' in header and 'href="/help"' in header
    form = html.split('id="scheduling"', 1)[1].split('</form>', 1)[0]
    assert 'href="/help"' not in form and 'href="/app/diary"' not in form
    assert ('href="/app/fleet"' in form) == fleet


@pytest.mark.parametrize('path,current',[
    ('/app/setup/guide',5),('/app/setup',5),
    ('/app/diary/dependants',5),('/app/crm/fields',5),('/app/crm/settings',5),('/app/setup/records',5),
    ('/app/diary',6),('/app/diary/settings',6),('/app/diary/intake',6),('/app/setup/diary',6),
    ('/app/money',7),('/app/invoices/settings',7),('/app/setup/charges',7)])
def test_native_setup_keeps_seven_positions_and_one_shared_shell(path,current):
    html=render(path,done=('records',) if current>5 else ())
    tags=Tags(html).tags
    buttons=[a for _,a in tags if 'data-step-name-toggle' in a]
    assert len(buttons)==7
    assert [i+1 for i,b in enumerate(buttons) if b.get('aria-current')=='step']==[current]
    assert all('completed' in b['aria-label'] for b in buttons[:4])
    assert len([a for tag,a in tags if tag=='body'])==1
    assert len([a for tag,a in tags if tag=='main'])==1
    assert len([a for _,a in tags if a.get('id')=='bc-panel'])==1
    assert 'data-onboarding-brand' in html and '>Automate<' in html
    assert 'data-onboarding-card' in html and 'data-native-form' in html
    assert 'data-setup-strip' not in html and 'class="app-sky' not in html
    assert html.count('/static/guided_setup.js?')==1
    assert html.count('/static/dirty_save.js?')==1 and 'id="setup-prefill"' in html
    assert 'data-in-app="true"' in html and 'data-onboarding-step=""' in html
    launcher=html.split('<button id="bc-bubble"',1)[1].split('</button>',1)[0]
    assert 'data-assistant-glass-logo' in launcher and '/static/img/blam8-assistant-glass.png?' in launcher
    assert '/static/img/blam8-assistant-animated.png?' not in html
    assert html.count('/static/assistant-glass-logo.js?')==1


@pytest.mark.parametrize('path,done,status,dismissed',[
    ('/app/',(),None,False),('/app/conversations',(),'active',False),
    ('/app/diary/settings',('diary','records','charges'),None,False),
    ('/app/diary/settings',(),None,True),
    ('/app/diary/settings',('diary','records','charges'),'complete',False)])
def test_normal_app_and_finished_or_dismissed_walk_keep_app_layout(path,done,status,dismissed):
    html=render(path,done,status,dismissed)
    tags=Tags(html).tags
    assert not any('data-onboarding-shell' in a for _,a in tags)
    assert any('app-sky' in a.get('class','') for tag,a in tags if tag=='main')
    assert 'data-native-form' in html
    assert 'data-assistant-glass-logo' not in html.split('<button id="bc-bubble"',1)[1].split('</button>',1)[0]


def test_pause_and_completed_review_keep_the_setup_shell():
    assert 'data-onboarding-shell' in render('/app/diary/settings',status='paused',dismissed=True)
    html=render('/app/setup/charges',done=('diary','records','charges'))
    assert 'data-onboarding-shell' in html
    buttons=[a for _,a in Tags(html).tags if 'data-step-name-toggle' in a]
    assert all('completed' in b['aria-label'] for b in buttons[:6])
