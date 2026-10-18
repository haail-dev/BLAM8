"""Shared by the ripples browser tests (water_page_browser.py, water_landing_browser.py): the real templates rendered with a stand-in
context (no app boot, no database, no network), and a tiny static server that serves the real scripts from app/static."""
import functools, http.server, re, tempfile, threading, types
from pathlib import Path
import jinja2

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="water-fixture-"))


class Anything:
    """Stands in for any context object: attributes, calls, items and arithmetic are all harmless."""
    def __getattr__(self, k):
        if k.startswith("__"): raise AttributeError(k)
        return Anything()
    def __call__(self, *a, **k): return Anything()
    def __getitem__(self, k): return Anything()
    def __iter__(self): return iter(())
    def __bool__(self): return False
    def __str__(self): return ""
    def __len__(self): return 0
    def __contains__(self, k): return False
    def __int__(self): return 0
    def __index__(self): return 0
    def __float__(self): return 0.0
    def _num(self, other=None): return 0
    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = __mod__ = _num
    def __lt__(self, o): return False
    __le__ = __gt__ = __ge__ = __lt__


def render(template, out, children=None, extra=None):
    """Render `template` (or a stand-in child page given in `children`) to TMP/out and return the path."""
    children = children or {}
    class Loader(jinja2.FileSystemLoader):
        def get_source(self, env, name):
            if name in children: return children[name], name, lambda: True
            return super().get_source(env, name)
    env = jinja2.Environment(loader=Loader(str(ROOT / "src/app" / "templates")), autoescape=jinja2.select_autoescape(["html"]))
    env.globals.update(csrf_token=lambda *a, **k: "x", asset_rev="test", base_url="https://blam8.com", invite_gate_active=lambda: False, gmail_slots=lambda: 0,
                       gmail_connect_open=lambda *a, **k: True, trial_email_cap=50, vapid_public_key="", voice_live_on=False, speech_voice_choices=lambda *a, **k: [],
                       bing_site_verification="", google_site_verification="")
    class Req:
        query_params = {}; cookies = {}; headers = {}
        state = types.SimpleNamespace(tenant_id="t1", user=Anything(), role="owner", nonce="")
        url = types.SimpleNamespace(path="/", scheme="https", netloc="blam8.com", query="")
        def url_for(self, name, **k): return "/static/" + str(k.get("path", ""))
    ctx = {"request": Req(), "tenant": Anything(), "user": Anything(), "concierge_on": False, "step_count": 3, "step_index": 1, "tuning": [],
           "invite_gate": False, "setups": [], "scenario_box": False}
    ctx.update(extra or {})
    for _ in range(60):
        try:
            html = env.get_template(template).render(**ctx); break
        except jinja2.UndefinedError as e:
            m = re.search(r"'([A-Za-z_0-9]+)' is undefined", str(e))
            if not m or m.group(1) in ctx: raise
            ctx[m.group(1)] = Anything()
    html = re.sub(r'<script[^>]*src="https://[^"]*"[^>]*></script>', "", html)   # no third-party scripts
    html = re.sub(r'<link[^>]*href="https://[^"]*"[^>]*>', "", html)
    (TMP / out).write_text(html, encoding="utf-8")
    return TMP / out


def serve(routes):
    """Serve `routes` ({url path: rendered file}) and /static/* from app/static; returns the base URL."""
    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?", 1)[0].split("#", 1)[0]
            if path in routes: return str(routes[path])
            if path.startswith("/static/"): return str(ROOT / "src/app" / "static" / path[len("/static/"):])
            return str(TMP / path.lstrip("/"))
        def log_message(self, *a): pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port
