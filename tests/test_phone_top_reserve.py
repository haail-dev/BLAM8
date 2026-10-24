"""The phone's reserved top strip stops at the camera cut-out (Kris 2026-10-24)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_reserve_trims_the_inset_to_the_camera_cut_out_but_never_below_a_plain_status_bar():
    css = _read("src/app/static/src/tw-input.css")
    assert ("--top-reserve: max(min(env(safe-area-inset-top, 0px), 20px), "
            "calc(env(safe-area-inset-top, 0px) - 11px));") in css


def test_navy_strips_and_header_bars_use_the_reserve():
    app = _read("src/app/templates/app/base.html")
    assert "height: calc(2.25rem + var(--top-reserve));" in app
    assert "padding-top: var(--top-reserve);\n    }\n    body::before" in app
    assert "height: var(--top-reserve);\n      background: rgb(var(--c-bg-navy" in app
    assert "#cz-head { padding-top: calc(0.625rem + var(--top-reserve)); }" in app
    assert app.count("top:calc(var(--top-reserve) + 3.25rem)") == 2
    site = _read("src/app/templates/base.html")
    assert "body { padding-top: var(--top-reserve);" in site
    assert "header.sticky { top: var(--top-reserve); }" in site
    onboarding = _read("src/app/templates/onboarding/base.html")
    assert "padding-top: var(--top-reserve);" in onboarding
    assert "env(safe-area-inset-top" not in onboarding


def test_pull_label_falls_back_where_no_tailwind_build_defines_the_reserve():
    js = _read("src/app/static/pull_refresh.js")
    assert "top: var(--top-reserve, env(safe-area-inset-top, 0px)); height: 0;" in js
