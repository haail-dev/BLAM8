"""Native correction Apply/check/checkbox form; backend fixtures, no live model."""

import asyncio
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import os
import time
import shutil
import pytest
from copy import deepcopy
from jinja2 import Environment, FileSystemLoader

from app import (
    knowledge_guidance as guide,
    knowledge_review,
    knowledge_source_review as review,
)
from app import (
    knowledge_note_edits as edits,
    knowledge_source_evidence,
    knowledge_fidelity,
)
from tests.test_knowledge_guidance_browser import chromium_binary
from tests.test_knowledge_review import context, entry

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width", [390, 1440])
def test_native_removal_apply_preserves_checkbox_value_and_other_local_edits(
    tmp_path, monkeypatch, width
):
    from app.config import settings

    monkeypatch.setattr(settings, "knowledge_source_review_enabled", True)
    original = "We handle enquiries.\n\nWe review each request.\n\nInclude a previous reference for a repeated enquiry."
    request = "Please remove the final repeated paragraph and keep the rest."
    first = entry("one", "Following up", original)
    other = entry("two", "Other note", "Original unrelated note.")
    ctx, row = context([first, other], brief="We handle enquiries.")
    source = knowledge_source_evidence.build(
        [{"url": "https://example.test/help", "text": original}]
    )
    row["payload"].update(
        source_snapshot=source["id"],
        draft_revision=1,
        source_units=knowledge_fidelity.inventory(source["pages"]),
        questions=[],
    )
    form = {
        "version": ctx["version"],
        "action_one": "keep",
        "action_two": "",
        "content_two": "Unrelated local edit stays exactly here.",
    }
    match = knowledge_review.review_view(ctx, row, form)["entries"][0]["matches"][0]
    match_name = "match_" + match["key"]
    form[match_name] = "keep_both"
    card = edits.scope(
        review.draft_card(row, source, {"two": {"content": form["content_two"]}}),
        ctx,
        row,
        form,
        "one",
        request,
    )
    line = card["lines"][0]
    raw = {
        "revision": 1,
        "changes": [
            {
                "id": "one",
                "content_hash": knowledge_source_evidence.digest(original),
                "insertions": [],
                "citations": [],
                "removals": [
                    card["knowledge_edit_scope"]["paragraphs"][-1]["reference"]
                ],
            }
        ],
        "overlaps": [],
        "questions": [],
        "edit_scope": edits.binding(card),
    }
    preview = review.validate(card, source, raw)
    payload = review.corrected_payload(
        row, source, raw, card=card, owner_request=request
    )
    assert payload["entries"][1]["content"] == "Original unrelated note."
    change = preview["changes"][0]
    applied = {
        "entries": [payload["entries"][0]],
        "patches": change["patches"],
        "summary": "Applied to draft; not approved or saved.",
    }
    updated = deepcopy(row)
    updated["payload"] = payload
    ctx2 = {**ctx, "version": "v2", "reviews": [updated]}
    checked = {
        **form,
        **change["patches"],
        "version": "v2",
        "content_one": payload["entries"][0]["content"],
    }
    shown = {**guide.present(ctx2, updated, checked), "patches": {}}
    confirmed = guide.apply(
        ctx2,
        updated,
        checked,
        shown["focus_hash"],
        {"intent": "keep", "reason": "", "answer": "", "target": "", "explanation": ""},
        confirmed_note_decision=True,
    )
    env = Environment(
        loader=FileSystemLoader(ROOT / "src/app/templates"), autoescape=True
    )

    def markup(context, row, fields):
        return env.get_template("_knowledge_review.html").render(
            review=knowledge_review.review_view(context, row, fields),
            csrf_token=lambda _: "offline",
            request={},
        )

    fixture = {
        "preview": preview,
        "applied": applied,
        "checked_html": markup(ctx2, updated, checked),
        "shown": shown,
        "confirmed": confirmed,
        "matchName": match_name,
        "request": request,
    }
    scripts = "".join(
        "<script>"
        + (ROOT / "src/app/static" / name).read_text(encoding="utf-8")
        + "</script>"
        for name in (
            "knowledge_source_review.js",
            "knowledge_guidance.js",
            "knowledge_onboarding_review.js",
        )
    )
    assertions = (ROOT / "tests/replays/knowledge_note_edit_native.js").read_text(
        encoding="utf-8"
    )
    page = tmp_path / "edit.html"
    page.write_text(
        "<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'\">"
        + '<div id="assistant-overlay" style="position:fixed;inset:0;background:white;z-index:99999">Assistant overlay</div><main id="knowledge-pending-reviews">'
        + markup(ctx, row, form)
        + '</main><pre id="result">Pending</pre><script>window.fixture='
        + json.dumps(fixture)
        + ";</script>"
        + '<script>window.blam8ConciergeClose=function(){document.getElementById("assistant-overlay").hidden=true;window.closedOverlay=true;};window.requests=[];window.fetch=async function(url,options){const f=window.fixture;let data;if(String(url).endsWith("/corrections/apply"))data=f.applied;else if(String(url).endsWith("/corrections"))data=f.preview;else if(String(url).endsWith("/guide")){let body=JSON.parse(options.body);window.requests.push(body);data=body.question==="Keep this note"?f.confirmed:f.shown;}else{if(options.body.get("operation")!=="preview")throw Error("No Save is allowed");data={review_html:f.checked_html,summary:"Checked; not saved"};}return {ok:true,json:async()=>structuredClone(data)};};</script>'
        + scripts
        + "<script>"
        + assertions
        + "</script>",
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    with (tmp_path / "chrome.log").open("w") as log:
        browser = subprocess.Popen(
            [
                chromium_binary(),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--no-first-run",
                "--no-proxy-server",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--remote-debugging-port=0",
                "--user-data-dir=" + str(profile),
                page.as_uri(),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            portfile = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not portfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert portfile.exists(), "Chromium debugging endpoint did not start"
            port = portfile.read_text().splitlines()[0]
            result = subprocess.run(
                [
                    shutil.which("node"),
                    str(ROOT / "tests/replays/knowledge_note_edit_native_driver.mjs"),
                    port,
                    str(width),
                ],
                capture_output=True,
                text=True,
                timeout=35,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            capture = json.loads(result.stdout)
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
    owner_confirm = next(
        r for r in capture["requests"] if r["question"] == "Keep this note"
    )
    accepted = asyncio.run(
        guide.turn(
            "tenant",
            ctx2,
            updated,
            owner_confirm["form"],
            owner_confirm["question"],
            owner_confirm["focus_hash"],
        )
    )
    assert accepted["patches"] == {"action_one": "keep"}
    assert (
        capture["form"]["action_one"] == "keep"
        and capture["form"]["content_two"] == form["content_two"]
    )
    assert any(
        k.startswith("match_") and v == "keep_both" for k, v in capture["form"].items()
    )
    assert updated["state"] == "pending"
