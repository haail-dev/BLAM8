"""Worked setups — /setups and /setups/{slug} (src/app/setups.py).

Every entry carries every section the page renders (validate() is the contract),
every page renders without a template error (a 500 on an indexed page), the
disclaimer that these are fictional rigs is on every page, and the pages ride
the sitemap and llms.txt like the rest of the public surface.

Run: python -m pytest tests/test_setups_pages.py -q
"""
import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from app import setups
from app.main import app, _PUBLIC_PAGES, templates


def esc(x: str) -> str:
    return str(escape(x))


@pytest.fixture
def client():
    yield TestClient(app)   # no `with`: startup events must not run


def test_every_setup_is_complete_and_honest():
    assert setups.validate() == []
    assert {s["slug"] for s in setups.SETUPS} == {"plumbers", "stonemasons", "kennels", "farriers", "hot-tub-servicing", "mot-garage", "swim-school", "driving-instructor"}
    for s in setups.SETUPS:
        assert "fictional" in s["business_blurb"].lower() or "fictional" in setups.DISCLAIMER
        assert s["run_doc"].startswith("docs/")


def test_index_lists_every_setup_with_the_disclaimer(client):
    r = client.get("/setups")
    assert r.status_code == 200
    for s in setups.SETUPS:
        assert f'href="/setups/{s["slug"]}"' in r.text
        assert esc(s["title"]) in r.text
    assert "fictional" in r.text
    assert "trusted by" not in r.text.lower()


@pytest.mark.parametrize("s", setups.SETUPS, ids=lambda s: s["slug"])
def test_detail_page_renders_every_section(client, s):
    r = client.get(f"/setups/{s['slug']}")
    assert r.status_code == 200
    for svc in s["diary"]["services"]:
        assert esc(svc["name"]) in r.text
        assert setups.TIMING_WORDS[svc["timing"]] in r.text
    for f in s["requirements"]["fields"]:
        assert esc(f["label"]) in r.text
    for st in s["crm"]["stages"]:
        assert esc(st) in r.text
    for rule in s["knowledge"]["rules"]:
        assert esc(rule["text"][:40]) in r.text
    assert esc(s["journey"]["steps"][0]["what"][:40]) in r.text
    assert esc(s["proven"]) in r.text and "fictional" in r.text
    assert 'href="/login"' in r.text


def test_unknown_setup_is_404(client):
    assert client.get("/setups/plumbers-from-mars").status_code == 404


def test_setups_ride_the_public_surface(client):
    assert "/setups" in _PUBLIC_PAGES
    for s in setups.SETUPS:
        assert f"/setups/{s['slug']}" in _PUBLIC_PAGES
    sm = client.get("/sitemap.xml").text
    assert "/setups/stonemasons" in sm
    llms = client.get("/llms.txt").text
    assert "/setups/kennels" in llms and "fictional" in llms.lower()


def test_duration_label_reads_naturally():
    assert setups.duration_label(45) == "45 min"
    assert setups.duration_label(480) == "8 hr"
    assert setups.duration_label(1440) == "overnight"
    assert setups.duration_label(2880) == "2 nights"


def test_for_businesses_links_to_the_worked_setups(client):
    r = client.get("/for-businesses")
    assert r.status_code == 200 and 'href="/setups"' in r.text
    for s in setups.SETUPS:
        assert f'href="/setups/{s["slug"]}"' in r.text


def test_a_fixed_gap_shows_what_was_built_and_keeps_the_finding(client):
    r = client.get("/setups/farriers")
    assert r.status_code == 200
    assert "What this run showed us we still owe this trade" in r.text
    assert "share one booking" in r.text                       # the finding stays on the record
    assert "Built since · 4 September 2026" in r.text
    assert "Every horse is its own record" in r.text
    assert "cycle is every six to eight weeks" in r.text     # an open gap renders as before


def test_validate_rejects_a_gap_without_text_or_with_stray_keys(monkeypatch):
    bad = dict(setups.SETUPS[0])
    bad["slug"] = "bad-gaps"
    bad["gaps"] = [{"built": "x"}, {"text": "y", "fixed": "z"}]
    monkeypatch.setattr(setups, "SETUPS", setups.SETUPS + [bad])
    problems = setups.validate()
    assert "bad-gaps: gap without text" in problems
    assert any(p.startswith("bad-gaps: gap has unknown keys") for p in problems)


def test_a_setup_with_nothing_to_attach_says_so():
    """A trade with no document to send validates with a no_document reason
    (the driving school shows the licence, never sends it); without either
    the validator still objects."""
    from app import setups
    entry = setups.BY_SLUG["driving-instructor"]
    assert not any(f["kind"] == "document" for f in entry["requirements"]["fields"])
    assert entry["requirements"]["no_document"]
    assert not [p for p in setups.validate() if p.startswith("driving-instructor")]
    stripped = {**entry, "requirements": {**entry["requirements"], "no_document": ""}}
    saved = setups.SETUPS[:]
    try:
        setups.SETUPS[:] = [stripped]
        assert any("no document field" in p for p in setups.validate())
    finally:
        setups.SETUPS[:] = saved


# ── The film (docs/video-playbook.md) ──────────────────────────────────────────

def test_kennels_carries_the_film_and_its_cue_sheet():
    v = setups.BY_SLUG["kennels"]["video"]
    assert v["src"].startswith(setups.VIDEO_DIR) and v["phone"]["src"].startswith(setups.VIDEO_DIR)
    assert v["run_doc"].startswith("docs/video-run-kennels")
    assert [c["t"] for c in v["chapters"]] == sorted(c["t"] for c in v["chapters"])
    assert v["chapters"][-1]["t"] < v["seconds"]


def test_validate_rejects_a_bad_film(monkeypatch):
    good = setups.BY_SLUG["kennels"]["video"]
    base = setups.SETUPS[:]

    def problems(video):
        bad = {**base[0], "slug": "bad-film", "video": video}
        monkeypatch.setattr(setups, "SETUPS", base + [bad])
        return [p for p in setups.validate() if p.startswith("bad-film")]

    assert problems(good) == []
    assert problems({**good, "poster": "/static/img/og-card.png"}) == \
        ["bad-film: video poster must be a jpg/png under /static/video/setups/"]
    assert any("past the end" in p for p in problems({**good, "seconds": 30}))
    assert any("in order" in p for p in problems({**good, "chapters": [{"t": 5, "text": "b"}, {"t": 5, "text": "c"}]}))
    assert any("unknown keys" in p for p in problems({**good, "bucket": "x"}))
    assert any("ISO date" in p for p in problems({**good, "filmed": "20 September 2026"}))
    assert any("run_doc" in p for p in problems({**good, "run_doc": "video-run.md"}))
    assert any("phone needs a summary" in p for p in problems({**good, "phone": {**good["phone"], "summary": ""}}))
    assert any("phone has unknown keys" in p for p in problems({**good, "phone": {**good["phone"], "chapters": []}}))
    assert problems({**good, "phone": None}) == []


def _land(root, url: str) -> None:
    p = root / url[len("/static/"):]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00")


def test_the_film_waits_for_its_files_then_lights_the_page(client, monkeypatch, tmp_path):
    """The clips are cut on Kris's machine and copied into app/static: until the
    desktop clip is on disk the page shows no player (and the site-wide share
    card); once it lands the hero shows the film, its chapters seek it, the
    poster is the share card, search gets a VideoObject, the index card and
    llms.txt say so — and the phone clip joins when its own files land."""
    monkeypatch.setattr(setups, "STATIC_DIR", tmp_path)
    k = setups.BY_SLUG["kennels"]
    v = k["video"]
    base = templates.env.globals["base_url"]
    assert setups.film(k) is None and "kennels" not in setups.films()
    r = client.get("/setups/kennels")
    assert r.status_code == 200 and "<video" not in r.text and "kennels-desktop.mp4" not in r.text
    assert f'property="og:image" content="{base}/static/img/og-card.png"' in r.text
    assert "VideoObject" not in r.text
    assert "-second film" not in client.get("/setups").text
    assert "Filmed:" not in client.get("/llms.txt").text

    _land(tmp_path, v["src"])
    _land(tmp_path, v["poster"])
    f = setups.film(k)
    assert f and "phone" not in f
    r = client.get("/setups/kennels")
    assert 'id="setup-film"' in r.text and f'src="{v["src"]}?v=' in r.text
    assert "kennels-phone.mp4" not in r.text
    assert f'property="og:image" content="{base}{v["poster"]}"' in r.text
    assert '"@type": "VideoObject"' in r.text and '"duration": "PT94S"' in r.text
    assert f'"contentUrl": "{base}{v["src"]}"' in r.text and '"uploadDate": "2026-09-20"' in r.text
    assert 'data-seek="84"' in r.text and "Today already knows" in r.text and "1:24" in r.text
    assert "Watch the 94-second film" in r.text
    assert "Filmed on the live system on 20 September 2026 on Little Paws Daycare" in r.text
    assert "Nothing is staged" in r.text

    _land(tmp_path, v["phone"]["src"])
    _land(tmp_path, v["phone"]["poster"])
    assert setups.film(k)["phone"] == v["phone"]
    r = client.get("/setups/kennels")
    assert f'src="{v["phone"]["src"]}?v=' in r.text and "From the owner's phone · 74 s." in r.text
    assert "Watch the 94-second film of the day" in client.get("/setups").text
    assert "Filmed: a 94-second video of the day" in client.get("/llms.txt").text


def test_static_video_serves_byte_ranges(client):
    """An iPhone will not play an mp4 from a server that ignores Range: the
    static mount answers 206 with exactly the slice asked for."""
    r = client.get("/static/video/blam8-loop.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.headers.get("accept-ranges") == "bytes"


def test_film_dates_and_times_read_naturally():
    assert setups.long_date("2026-09-20") == "20 September 2026"
    assert setups.clock(94) == "1:34" and setups.clock(1) == "0:01" and setups.clock(0) == "0:00"



def test_the_kennels_page_lists_what_was_proven_since(client):
    # Kris 2026-09-25: update the kennels page. Each line is a verified row.
    html = client.get("/setups/kennels").text
    assert "data-setup-since" in html and "Proven on a live kennels since" in html
    assert "becomes two bookings" in html and "fully booked" in html


def test_a_trade_name_mid_sentence_keeps_its_acronym(client):
    """Garage walk, 1 Oct 2026: the tab title read "BLAM8 for mot and service
    garages" — the whole trade name was lower-cased. A word in capitals stays."""
    from app import setups
    assert setups.lower_trade("MOT and service garages") == "MOT and service garages"
    assert setups.lower_trade("Plumbing & heating engineers") == "plumbing & heating engineers"
    assert setups.lower_trade("Kennels, dog daycare & boarding") == "kennels, dog daycare & boarding"
    r = client.get("/setups/mot-garage")
    assert "BLAM8 for MOT and service garages" in r.text
    assert "for mot and" not in r.text
