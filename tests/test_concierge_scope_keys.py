"""The assistant widget's browser storage is scoped to the tenant.

Live 2026-09-04: signing a second tenant into the same browser inherited the
first one's "setup done" flag and snooze stamps, so the whole post-onboarding
tuning walk-through never appeared for the new business — and the chat thread
carried over with it. Browser storage is per-browser; the account is not.

Every key the widget writes now carries a per-tenant suffix (data-scope, set
from request.state.tenant_id in require_tenant). Signed out, the scope is empty
and the keys read exactly as they always did.

Run: python -m pytest tests/test_concierge_scope_keys.py -q
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
TPL = (ROOT / "src/app" / "templates" / "_concierge.html").read_text(encoding="utf-8")


def test_every_storage_key_goes_through_the_scoper():
    # A bare "blam8_…" string literal is a key that would be shared across tenants.
    bare = [m for m in re.findall(r'(?<!k\()"blam8_[a-z0-9_]+"', JS)]
    assert bare == [], bare


def test_the_scoper_is_a_suffix_and_a_no_op_when_signed_out():
    fn = JS.split("function k(name)")[1].split("\n")[0]
    assert 'SCOPE ? name + ":" + SCOPE : name' in fn


def test_the_widget_is_handed_its_tenant_scope():
    assert 'data-scope="{{ (request.state.tenant_id or \'\')[:12] }}"' in TPL
    assert 'root.getAttribute("data-scope")' in JS


def test_require_tenant_publishes_the_scope():
    auth = (ROOT / "src/app" / "auth.py").read_text(encoding="utf-8")
    assert "request.state.tenant_id = tenant_id" in auth
