"""Trusted native clicks through the real backlog form, sync wrapper and handler."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import subprocess
import threading
import shutil
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
import pytest

from app import backlog, onboarding, device_sync
from tests.test_device_sync import cluster, sync_db, sync_case, sync_app
from tests.test_knowledge_guidance_browser import chromium_binary, devtools_port

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "mode", [0, 1, 2], ids=["decide-later", "cancel-then-retry", "invalid-then-retry"]
)
def test_trusted_click_submits_backlog_choice_once(
    sync_app, monkeypatch, tmp_path, mode
):
    app, _, ctx = sync_app
    connection = str(uuid4())
    choices, submitted = [], []
    monkeypatch.setattr(backlog.db, "get_member", lambda *a, **kw: {})
    from app import people

    monkeypatch.setattr(people, "can_do", lambda *a: True)
    monkeypatch.setattr(people, "may_read_mailbox", lambda *a: True)
    monkeypatch.setattr(backlog.db, "mailbox_import_access", lambda *a, **kw: {})

    def choose(tenant, mailbox, user, decision, expected):
        choices.append((tenant, mailbox, user, decision, expected))
        return {"decision": decision}

    monkeypatch.setattr(backlog.db, "choose_mailbox_import", choose)
    monkeypatch.setattr(
        backlog,
        "start_import",
        lambda *a: pytest.fail("Decide later must not start import"),
    )
    env = Environment(
        loader=FileSystemLoader(ROOT / "src/app/templates"), autoescape=True
    )

    @app.get("/submission-state")
    def state():
        return {"choices": choices, "submitted": submitted}

    def page():
        card = env.get_template("_backlog_status.html").render(
            backlog_status={
                "state": "not_started",
                "message": "Decided later" if choices else "No choice yet",
                "retry": not choices,
                "connection_id": connection,
                "expected_run": "expected-run",
            },
            backlog_settings=False,
            request={},
            csrf_token=lambda _: "test-csrf",
        )
        if mode == 2 and not choices:
            card = card.replace(
                "</form>", '<input name="validation" required value="ready"></form>'
            )
        response = HTMLResponse(
            "<!doctype html><main>"
            + card
            + "</main>"
            + (
                '<script>document.addEventListener("submit",function(){if(!window.invalidatedOnce){window.invalidatedOnce=true;queueMicrotask(function(){document.querySelector("[name=validation]").value="";});}},true);document.querySelector("[name=validation]").addEventListener("invalid",function(){window.invalidReported=true;});</script>'
                if mode == 2 and not choices
                else ""
            )
            + '<script src="/static/device_sync.js"></script>'
            + (
                '<script>document.querySelector("form").addEventListener("submit",function(e){if(!window.cancelledOnce){window.cancelledOnce=true;e.preventDefault();}});</script>'
                if mode == 1 and not choices
                else ""
            )
        )
        response.set_cookie("bl_csrf", "test-csrf")
        return response

    # Override the small fixture page with the shipped backlog template.
    app.router.routes.insert(0, APIRoute("/onboarding/mailbox", page))
    routes = APIRouter(route_class=device_sync.SyncRoute)

    @routes.post("/onboarding/mailbox/backlog")
    async def decide(request: Request):
        submitted.append(dict(await request.form()))
        return await onboarding.mailbox_backlog(request, ctx)

    app.include_router(routes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def log_message(self, *_):
            pass

        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            with TestClient(app) as client:
                result = client.request(
                    self.command,
                    self.path,
                    content=body,
                    headers=dict(self.headers),
                    follow_redirects=False,
                )
            self.send_response(result.status_code)
            for key, value in result.headers.multi_items():
                if key.lower() not in ("connection", "transfer-encoding"):
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(result.content)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    profile = tmp_path / "browser-profile"
    command = [
        chromium_binary(),
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--no-first-run",
        "--no-proxy-server",
        "--remote-debugging-port=0",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--user-data-dir=" + str(profile),
        f"http://127.0.0.1:{server.server_port}/onboarding/mailbox",
    ]
    with (tmp_path / "chrome.log").open("w") as log:
        browser = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            port = devtools_port(profile)
            result = subprocess.run(
                [
                    shutil.which("node"),
                    str(ROOT / "tests/replays/mailbox_submit_native.mjs"),
                    port,
                    str(server.server_port),
                    str(mode),
                ],
                capture_output=True,
                text=True,
                timeout=35,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert len(choices) == len(submitted) == 1
            form = submitted[0]
            assert form["decision"] == "later" and form["connection_id"] == connection
            assert (
                form["expected_run"] == "expected-run"
                and form["csrf_token"] == "test-csrf"
            )
            assert form["_sync_channel"] == "flow" and int(form["_sync_revision"]) >= 1
            assert choices[0] == (
                ctx.tenant_id,
                connection,
                ctx.user_id,
                "later",
                "expected-run",
            )
            # Actual SQL operation receipt prevents executing the choice twice.
            with TestClient(app, cookies={"bl_csrf": "test-csrf"}) as client:
                assert (
                    client.post(
                        "/onboarding/mailbox/backlog", data=form, follow_redirects=False
                    ).status_code
                    == 303
                )
            assert len(choices) == len(submitted) == 1

        finally:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                    stdout=log,
                    stderr=log,
                    timeout=10,
                )
            else:
                browser.terminate()
            browser.wait(timeout=10)
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
