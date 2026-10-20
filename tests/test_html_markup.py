"""Selector regressions: stylesheet lookalikes cannot supply DOM evidence."""
import pytest

from tests.html_markup import markup
from tests.test_pull_refresh import _assert_shell_drag_opt_outs
from tests.test_view_as import _assert_banner_below_bar
from tests.test_strip_tap_spinner import _assert_door_icons, _strip


CSS = '<style>nav[aria-label="Primary"] > a { color: red } [data-viewing-as] { display: none }</style>'


def test_only_real_tags_supply_attributes_and_order():
    shell = markup(CSS + '<header id="app-bar"></header><div data-viewing-as></div>'
                   '<nav aria-label="Primary" data-pull-refresh="off"></nav>')
    assert shell.one('nav', **{'aria-label': 'Primary'}).attrs['data-pull-refresh'] == 'off'
    assert shell.one('header').start < shell.one(**{'data-viewing-as': None}).start
    with pytest.raises(AssertionError):
        markup(CSS).one('nav', **{'aria-label': 'Primary'})
    with pytest.raises(AssertionError):
        markup(CSS).one(**{'data-viewing-as': None})
    missing = markup(CSS + '<nav aria-label="Primary"></nav>')
    assert missing.one('nav').attrs.get('data-pull-refresh') != 'off'
    reversed_order = markup(CSS + '<div data-viewing-as></div><header id="app-bar"></header>')
    assert reversed_order.one('header').start > reversed_order.one(**{'data-viewing-as': None}).start


def _doors():
    return ''.join('<a href="/' + label.lower() + '"><span class="relative inline-flex h-6 w-6" data-nav-icon><svg class="h-6 w-6"></svg></span>'
                   + label + '</a>' for label in ('Today', 'Inbox', 'Money', 'CRM', 'Diary'))


@pytest.mark.parametrize('damage', ['missing_holder', 'nested_holder', 'label_in_holder', 'missing_door'])
def test_door_contract_rejects_broken_markup_despite_css_lookalikes(damage):
    doors = _doors()
    _assert_door_icons(_strip(CSS + '<nav aria-label="Primary">' + doors + '</nav>'))
    if damage == 'missing_holder':
        doors = doors.replace('data-nav-icon', 'data-other', 1)
    elif damage == 'nested_holder':
        doors = doors.replace('<span class="relative inline-flex h-6 w-6" data-nav-icon><svg class="h-6 w-6"></svg></span>',
                              '<span><span class="relative inline-flex h-6 w-6" data-nav-icon><svg class="h-6 w-6"></svg></span></span>', 1)
    elif damage == 'label_in_holder':
        doors = doors.replace('</span>Today', 'Today</span>', 1)
    else:
        doors = doors[:doors.rfind('<a ')]
    with pytest.raises(AssertionError):
        _assert_door_icons(_strip(CSS + '<nav aria-label="Primary">' + doors + '</nav>'))


def test_actual_drag_and_banner_contracts_reject_missing_opt_out_and_wrong_order():
    rails = ('<nav id="menu" data-pull-refresh="off"></nav>'
             '<div aria-label="Diary rail" data-pull-refresh="off"></div>'
             '<nav aria-label="Primary" data-pull-refresh="off"></nav>')
    _assert_shell_drag_opt_outs(CSS + rails)
    for index in range(3):
        parts = rails.split('data-pull-refresh="off"')
        parts[index:index + 2] = [''.join(parts[index:index + 2])]
        broken = 'data-pull-refresh="off"'.join(parts)
        with pytest.raises(AssertionError):
            _assert_shell_drag_opt_outs(CSS + broken)
    with pytest.raises(AssertionError):
        _assert_banner_below_bar(markup(CSS + '<div data-viewing-as></div><header id="app-bar"></header>'))
