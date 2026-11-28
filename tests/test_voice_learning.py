"""Voice auto-learning from sent mail (build directive 2026-07-25).

Pins the contracts that make the feature safe to trust:
  * filtering — only the user's own, human-directed, fresh writing is analysed
    (no forwards, no noreply traffic, no quoted history, no other senders);
  * merging — batch profiles combine by majority / mean / dedupe;
  * mapping — an accepted profile fills exactly the config fields the drafter
    reads, and never clobbers a hand-pasted examples box;
  * lifecycle — run_learning always lands on a UI-renderable status
    (insufficient / failed / ready) and never raises;
  * the onboarding voice step — review screen when a profile is ready, manual
    form fallback otherwise, hybrid provenance on hand-adjustment;
  * privacy — voice analysis calls log only the envelope (log_mode='meta'),
    so raw sent mail never lands in llm_calls.

Run: python -m pytest tests/test_voice_learning.py -q
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.onboarding as onboarding
from app import db, llm, voice_learning as vl
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from app.security import require_csrf

T = "t1"


# ── filtering: what counts as the user's own writing ─────────────────────────

def _msg(**over):
    base = {"subject": "Re: your quote", "from_address": "julius@acme.co.uk",
            "to_addresses": ["customer@example.com"],
            "body": "Hi Sarah,\n\nThanks for reaching out — happy to help with that.\n\nCheers,\nJulius"}
    return {**base, **over}


def test_own_text_cuts_quoted_history():
    body = ("Thanks Tom — Tuesday works.\n\nCheers,\nJulius\n"
            "On Mon, 20 Jul 2026 at 10:12, Tom Smith wrote:\n> can you do Tuesday?")
    assert vl._own_text(body) == "Thanks Tom — Tuesday works.\n\nCheers,\nJulius"


@pytest.mark.parametrize("marker", [
    "-----Original Message-----", "________________________",
    "From: Tom <tom@x.com>", "> quoted line",
])
def test_own_text_cuts_at_every_marker(marker):
    assert vl._own_text(f"My words here, long enough to count.\n{marker}\nold text") \
        == "My words here, long enough to count."


def test_usable_text_accepts_a_normal_sent_reply():
    assert vl.usable_text(_msg(), "julius@acme.co.uk")


def test_usable_text_rejects_other_senders_shared_mailbox():
    assert vl.usable_text(_msg(from_address="colleague@acme.co.uk"), "julius@acme.co.uk") is None


def test_usable_text_rejects_forwards_and_automated_recipients():
    assert vl.usable_text(_msg(subject="FW: invoice"), "julius@acme.co.uk") is None
    assert vl.usable_text(_msg(subject="Fwd: invoice"), "julius@acme.co.uk") is None
    assert vl.usable_text(_msg(to_addresses=["noreply@bank.com"]), "julius@acme.co.uk") is None
    assert vl.usable_text(_msg(to_addresses=["notifications@github.com"]), "julius@acme.co.uk") is None


def test_usable_text_rejects_too_little_fresh_text():
    assert vl.usable_text(_msg(body="Thanks!\n> the whole previous thread"), "julius@acme.co.uk") is None


# ── merging batch profiles ───────────────────────────────────────────────────

def _profile(**over):
    base = {"tone": "warm", "formality_level": 2, "greeting_style": "Hi [name]",
            "sign_off": "Cheers,\nJulius", "sign_off_name": "Julius",
            "sentence_style": "short and punchy", "average_length": "brief",
            "emoji_usage": "none", "common_phrases": ["happy to help"],
            "pricing_style": "direct quote", "closing_style": "warm",
            "personality_notes": "Friendly and direct.",
            "example_tone_description": "Warm but brisk."}
    return {**base, **over}


def test_merge_majority_mean_and_dedupe():
    merged = vl.merge_profiles([
        _profile(),
        _profile(tone="professional", formality_level=4,
                 common_phrases=["Happy to help", "give me a shout"]),
        _profile(formality_level=3),
    ])
    assert merged["tone"] == "warm"                       # 2 of 3
    assert merged["formality_level"] == 3                 # round(mean(2,4,3))
    assert merged["common_phrases"] == ["happy to help", "give me a shout"]  # case-insens dedupe
    assert merged["sign_off_name"] == "Julius"
    assert merged["example_tone_description"] == "Warm but brisk."


# ── profile → config mapping ─────────────────────────────────────────────────

def test_profile_to_config_fills_the_drafter_fields():
    cfg = vl.profile_to_config(_profile())
    assert cfg["tone"] == "warm, short and punchy"
    assert cfg["agent_name"] == "Julius"
    assert cfg["sign_off"] == "Cheers,\nJulius"
    assert "Hi [name]" in cfg["voice_notes"]
    assert "Never use emoji." in cfg["voice_notes"]
    assert "happy to help" in cfg["voice_notes"]


def test_profile_to_config_team_signature_leaves_agent_name_unset():
    assert vl.profile_to_config(_profile(sign_off_name=""))["agent_name"] is None


def test_profile_to_config_normalises_the_sign_off():
    """The analysis sometimes joins two alternatives with ' / ' and a stored value can
    carry CRLF; the accepted value is the one canonical form (src/app/sign_off.py)."""
    two = _profile(sign_off="Many thanks,\r\nSam / Kind regards,\r\nSam Jones")
    assert vl.profile_to_config(two)["sign_off"] == "Many thanks,\nSam"
    crlf = _profile(sign_off="Best regards,\r\nThe Example Team \r\n")
    assert vl.profile_to_config(crlf)["sign_off"] == "Best regards,\nThe Example Team"
    assert vl.profile_to_config(_profile(sign_off="Cheers,\nJulius"))["sign_off"] == "Cheers,\nJulius"
    assert vl.profile_to_config(_profile(sign_off="  \r\n "))["sign_off"] is None
    assert vl.profile_to_config(_profile(sign_off=""))["sign_off"] is None


# ── lifecycle: run_learning always lands on a renderable status ──────────────

@pytest.fixture
def stored(monkeypatch):
    """Capture every tenant_config upsert; block real reads."""
    writes: list[dict] = []
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: writes.append(cfg))
    monkeypatch.setattr(db, "get_tenant_config",
                        lambda tid, client=None: {"business_brief": "A firm."})
    return writes


def test_run_learning_insufficient_falls_back(monkeypatch, stored):
    async def few(tid, mid=None):
        return [_msg() for _ in range(5)], "julius@acme.co.uk"
    monkeypatch.setattr(vl, "fetch_sent_corpus", few)
    assert asyncio.run(vl.run_learning(T)) == "insufficient"
    assert stored[-1]["voice_learning_status"] == "insufficient"
    assert stored[-1]["voice_emails_analysed"] == 5


def test_run_learning_fetch_failure_is_contained(monkeypatch, stored):
    async def boom(tid, mid=None):
        raise RuntimeError("graph 503")
    monkeypatch.setattr(vl, "fetch_sent_corpus", boom)
    assert asyncio.run(vl.run_learning(T)) == "failed"
    assert stored[-1] == {"voice_learning_status": "failed"}


def test_run_learning_analysis_failure_is_contained(monkeypatch, stored):
    async def enough(tid, mid=None):
        return [_msg() for _ in range(25)], "julius@acme.co.uk"

    async def boom(tid, texts):
        raise RuntimeError("llm down")
    monkeypatch.setattr(vl, "fetch_sent_corpus", enough)
    monkeypatch.setattr(vl, "analyse", boom)
    assert asyncio.run(vl.run_learning(T)) == "failed"
    assert stored[-1] == {"voice_learning_status": "failed"}


def test_run_learning_happy_path_parks_a_ready_profile(monkeypatch, stored):
    async def enough(tid, mid=None):
        return [_msg() for _ in range(25)], "julius@acme.co.uk"

    async def analysed(tid, texts):
        assert len(texts) == 25
        return _profile()

    async def sample(tid, profile, brief):
        assert brief == "A firm."
        return "Hi Sarah,\n\nCheers,\nJulius"
    monkeypatch.setattr(vl, "fetch_sent_corpus", enough)
    monkeypatch.setattr(vl, "analyse", analysed)
    monkeypatch.setattr(vl, "generate_sample", sample)
    assert asyncio.run(vl.run_learning(T)) == "ready"
    final = stored[-1]
    assert final["voice_learning_status"] == "ready"
    assert final["voice_profile"]["sign_off_name"] == "Julius"
    assert final["voice_sample_draft"].startswith("Hi Sarah")
    assert final["voice_emails_analysed"] == 25


# ── apply_profile: acceptance writes the real fields ─────────────────────────

def test_apply_profile_fills_fields_and_examples(monkeypatch):
    writes = []
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {
        "voice_profile": _profile(), "voice_learning_status": "ready",
        "voice_sample_draft": "Hi Sarah…", "examples": None})
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: writes.append(cfg))
    assert vl.apply_profile(T) is True
    w = writes[-1]
    assert w["tone"] == "warm, short and punchy"
    assert w["examples"] == "Hi Sarah…"          # sample fills the empty box
    assert w["voice_profile_source"] == "auto_learned"
    assert w["voice_learning_status"] == "accepted"


def test_apply_profile_never_clobbers_pasted_examples(monkeypatch):
    writes = []
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {
        "voice_profile": _profile(), "voice_learning_status": "ready",
        "voice_sample_draft": "generated", "examples": "their real pasted replies"})
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: writes.append(cfg))
    assert vl.apply_profile(T) is True
    assert "examples" not in writes[-1]


def test_apply_profile_without_a_ready_profile_is_a_noop(monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {})
    assert vl.apply_profile(T) is False


# ── discard: revocation forgets the learned artefacts, keeps the settings ────

def test_discard_profile_nulls_learned_artefacts_only(monkeypatch):
    writes = []
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: writes.append(cfg))
    vl.discard_profile(T)
    w = writes[-1]
    assert w == {"voice_profile": None, "voice_sample_draft": None,
                 "voice_learning_status": None, "voice_emails_analysed": None,
                 "voice_learned_at": None, "voice_profile_source": None}
    for live_field in ("tone", "agent_name", "sign_off", "voice_notes", "examples"):
        assert live_field not in w      # live voice config is never touched


# ── onboarding voice step routes ─────────────────────────────────────────────

@pytest.fixture
def client():
    ctx = lambda: TenantCtx(tenant_id=T, client=None)  # noqa: E731
    app.dependency_overrides[require_tenant] = ctx
    app.dependency_overrides[require_onboarded] = ctx
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def _voice_env(monkeypatch, cfg, connected=True):
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: dict(cfg))
    monkeypatch.setattr(db, "get_connection_status",
                        lambda tid, client=None: {"id": "c1"} if connected else None)


def test_voice_step_shows_review_when_profile_ready(client, monkeypatch):
    _voice_env(monkeypatch, {
        "voice_learning_status": "ready", "voice_profile": _profile(),
        "voice_sample_draft": "Hi Sarah,\n\nCheers,\nJulius", "voice_emails_analysed": 247})
    r = client.get("/onboarding/voice")
    assert r.status_code == 200
    assert "I've analysed 247 of your sent emails." in r.text
    assert "Looks good — continue" in r.text
    assert "/onboarding/voice/accept" in r.text
    assert "Cheers,\nJulius" in r.text                      # the sample draft renders
    assert "This is a starting point" not in r.text          # 247 ≥ soft threshold


def test_voice_step_soft_copy_under_100_emails(client, monkeypatch):
    _voice_env(monkeypatch, {
        "voice_learning_status": "ready", "voice_profile": _profile(),
        "voice_sample_draft": "Hi", "voice_emails_analysed": 47})
    r = client.get("/onboarding/voice")
    assert "I've analysed 47 of your sent emails." in r.text
    assert "This is a starting point" in r.text


def test_voice_step_is_opt_in_never_auto_spawns(client, monkeypatch):
    """Connected + never run = an OFFER, not an automatic read of their mail."""
    spawned = []
    _voice_env(monkeypatch, {})
    monkeypatch.setattr(vl, "spawn", lambda tid, mid=None: spawned.append(tid))
    r = client.get("/onboarding/voice")
    assert spawned == []                                     # nothing reads sent mail uninvited
    assert "Learn from sent emails" in r.text
    assert "/onboarding/voice/learn" in r.text
    assert "voice-form" in r.text                            # manual form stays available
    assert "Analysing your sent emails" not in r.text


def test_voice_learn_opt_in_spawns_when_connected(client, monkeypatch):
    spawned = []
    _voice_env(monkeypatch, {})
    monkeypatch.setattr(vl, "spawn", lambda tid, mid=None: spawned.append(tid))
    r = client.post("/onboarding/voice/learn", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/onboarding/voice")
    assert spawned == [T]


def test_voice_learn_without_a_mailbox_is_a_noop(client, monkeypatch):
    spawned = []
    _voice_env(monkeypatch, {}, connected=False)
    monkeypatch.setattr(vl, "spawn", lambda tid, mid=None: spawned.append(tid))
    client.post("/onboarding/voice/learn", follow_redirects=False)
    assert spawned == []


def test_voice_step_manual_escape_hatch_skips_the_wait(client, monkeypatch):
    _voice_env(monkeypatch, {"voice_learning_status": "running"})
    r = client.get("/onboarding/voice?manual=1")
    assert "voice-form" in r.text and "Analysing your sent emails" not in r.text


def test_voice_step_manual_form_when_no_mailbox(client, monkeypatch):
    _voice_env(monkeypatch, {}, connected=False)
    r = client.get("/onboarding/voice")
    assert "voice-form" in r.text
    assert "Analysing" not in r.text


def test_voice_step_insufficient_message(client, monkeypatch):
    _voice_env(monkeypatch, {"voice_learning_status": "insufficient"})
    r = client.get("/onboarding/voice")
    assert "Not enough sent emails to learn from yet" in r.text


def test_voice_status_endpoint(client, monkeypatch):
    _voice_env(monkeypatch, {"voice_learning_status": "running"})
    assert client.get("/onboarding/voice/status").json() == {"status": "running"}


def _voice_save_env(monkeypatch,cfg):
    from types import SimpleNamespace
    from app import voice_completion,setup_held
    _voice_env(monkeypatch,cfg)
    monkeypatch.setattr(db,'get_member',lambda *a,**kw:None)
    writes=[]
    def rpc(name,args):
        assert name=='save_onboarding_voice' and args['p_expected']==voice_completion.snapshot(cfg)
        writes.append(args['p_values']);cfg.update(args['p_values'])
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=True))
    monkeypatch.setattr(db,'db',lambda:SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(setup_held,'mark_used',lambda *a,**kw:None)
    return writes


def _voice_post(client,cfg,values,endpoint='/onboarding/voice'):
    from app import voice_completion
    return client.post(endpoint,data={**values,'voice_expected':json.dumps(voice_completion.snapshot(cfg))},follow_redirects=False)


def test_voice_accept_applies_and_continues(client,monkeypatch):
    cfg={'voice_profile':_profile(),'voice_learning_status':'ready'}
    writes=_voice_save_env(monkeypatch,cfg)
    result=_voice_post(client,cfg,{},'/onboarding/voice/accept')
    assert (result.status_code,result.headers['location'])==(303,'/onboarding/knowledge')
    assert writes[0]['voice_profile_source']=='auto_learned'


def test_voice_accept_without_profile_returns_to_step(client,monkeypatch):
    cfg={};writes=_voice_save_env(monkeypatch,cfg)
    assert _voice_post(client,cfg,{},'/onboarding/voice/accept').status_code==409 and not writes


def test_voice_manual_save_marks_hybrid_over_a_learned_profile(client,monkeypatch):
    cfg={'voice_profile':_profile(),'voice_learning_status':'ready'}
    writes=_voice_save_env(monkeypatch,cfg)
    result=_voice_post(client,cfg,{'agent_name':'Office team','tone':'brisk','sign_off':'Regards'})
    assert result.headers['location']=='/onboarding/knowledge'
    assert writes[0]['voice_profile_source']=='hybrid' and writes[0]['tone']=='brisk'


def test_voice_manual_save_without_profile_is_manual(client,monkeypatch):
    cfg={};writes=_voice_save_env(monkeypatch,cfg)
    assert _voice_post(client,cfg,{'agent_name':'Office team','tone':'brisk','sign_off':'Regards'}).status_code==303
    assert writes[0]['voice_profile_source']=='manual'


def test_voice_save_stores_the_sign_off_in_one_canonical_form(client,monkeypatch):
    cfg={};writes=_voice_save_env(monkeypatch,cfg)
    values={'agent_name':'Office team','tone':'brisk','sign_off':'Kind regards,\r\nThe team \r\n'}
    assert _voice_post(client,cfg,values).status_code==303
    assert writes[0]['sign_off']=='Kind regards,\nThe team'
    assert _voice_post(client,cfg,{**values,'sign_off':' \r\n '}).status_code==422
    assert _voice_post(client,cfg,{'agent_name':'Office team','tone':'brisk'}).status_code==422
    assert len(writes)==1


def test_settings_voice_discard_route(client, monkeypatch):
    writes = []
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: writes.append(cfg))
    r = client.post("/app/settings/voice-discard", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/app/settings?voice=discarded")
    assert writes[-1]["voice_profile"] is None


# ── privacy: analysis calls must not persist the corpus ──────────────────────

class _FakeMsg:
    model = "claude-sonnet-4-6"
    stop_reason = "end_turn"
    usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
    content = [type("B", (), {"type": "text", "text": json.dumps({"ok": True})})()]


class _FakeClient:
    class messages:
        @staticmethod
        async def create(**kwargs):
            return _FakeMsg()


def test_meta_log_mode_keeps_sent_mail_out_of_llm_calls(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "insert_llm_call", lambda row: rows.append(row))
    monkeypatch.setattr(llm.settings, "llm_log_mode", "full")
    asyncio.run(llm.create(client=_FakeClient(), tenant_id=T, operation="voice_learning",
                           module="voice", log_mode="meta", model="m", max_tokens=5,
                           messages=[{"role": "user", "content": "RAW SENT MAIL"}]))
    assert len(rows) == 1
    assert "request" not in rows[0] and "response" not in rows[0]   # envelope only
    assert rows[0]["operation"] == "voice_learning"


def test_full_mode_unchanged_for_ordinary_calls(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "insert_llm_call", lambda row: rows.append(row))
    monkeypatch.setattr(llm.settings, "llm_log_mode", "full")
    asyncio.run(llm.create(client=_FakeClient(), tenant_id=T, operation="draft",
                           model="m", max_tokens=5,
                           messages=[{"role": "user", "content": "hi"}]))
    assert "request" in rows[0]


def test_generate_sample_never_claims_availability(monkeypatch):
    """The voice-step sample answers "availability next week?" with no diary in
    sight. On the rig (30 Sep, first-sign-up film) it said "next week is looking
    reasonable": a timing claim rule 2c forbids in real replies, on camera. The
    prompt now says to check the diary and come back, never that a time is free."""
    seen = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return _FakeMsg()
    monkeypatch.setattr(vl.llm, "create", fake_create)
    monkeypatch.setattr(vl.agent, "client", lambda: None)
    monkeypatch.setattr(vl.usage, "record_anthropic", lambda *a, **k: None)
    asyncio.run(vl.generate_sample(T, {}, "A firm."))
    system = seen["system"]
    assert "never say or imply when the business is free" in system
    assert "check the diary and come back" in system
    assert "offer to confirm availability" not in system
