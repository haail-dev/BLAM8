"""The Settings trial banner never says "trial has ended" to a comped business.

Live 2026-09-30 (Hartwell rig, comp_access 'indefinite'): trial.evaluate() returned status
'comped', active True, but the banner only knew active / grace_active / converted, so the
comp fell to the else branch: "Your free trial has ended — 100-email limit reached. Choose a
plan to switch the agent back on." while the agent kept running. A comp now hides the banner,
like a paid plan. The block is rendered straight from the template so no route setup is needed.

Run: python -m pytest tests/test_settings_trial_banner.py -q
"""
import re
from pathlib import Path

from jinja2 import Environment

SRC = (Path(__file__).resolve().parents[1] / "src/app/templates/app/settings.html").read_text(encoding="utf-8")
BLOCK = SRC[SRC.index("<!-- Free-trial status"):]
BLOCK = BLOCK[:BLOCK.index("</section>") + len("</section>")]
BLOCK = BLOCK[:BLOCK.index("{% if trial")] + BLOCK[BLOCK.index("{% if trial"):] + "\n{% endif %}"


def _render(**trial):
    return Environment().from_string(BLOCK).render(trial=trial, gate_on=True)


def test_an_expired_trial_says_so():
    html = _render(status="expired_cap", active=False, cap=100, emails_used=117, days_remaining=0)
    assert "Your free trial has ended" in html


def test_a_comp_shows_no_trial_banner():
    html = _render(status="comped", active=True, cap=100, emails_used=117, days_remaining=None)
    assert "trial" not in re.sub(r"<!--.*?-->", "", html, flags=re.S).lower()


def test_a_paid_plan_shows_no_trial_banner():
    html = _render(status="converted", active=True, cap=100, emails_used=0, days_remaining=None)
    assert "trial" not in re.sub(r"<!--.*?-->", "", html, flags=re.S).lower()


def test_an_active_trial_shows_its_days():
    html = _render(status="active", active=True, cap=100, emails_used=5, days_remaining=9)
    assert "9 days left" in html
