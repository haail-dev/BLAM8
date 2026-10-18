"""Shared, explicit knowledge admission. Suggestions never become saved facts here.

Overlap scores are review hints, not semantic equivalence. An absent answer is
unknown; only a reviewer can confirm coverage or mark a topic not applicable.
"""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from app import db, knowledge_coverage as coverage

STATES = {"covered", "partial", "unknown", "not_applicable"}
# Generic dimensions, never sector requirements. No answer is inferred from text.
QUESTIONS = {
    "prices": ["What are the main prices and units?", "Do prices vary with quantity or size?",
               "Do prices vary by location or travel?", "Do prices vary with timing or out-of-hours work?"],
    "rules": ["What conditions or exceptions must be checked before agreeing to work?"],
    "process": ["What steps and information are needed for a first request?"],
    "cadence": ["Does work repeat, and how are follow-ups arranged?"],
    "payment": ["When and how is payment due, including any deposits?"],
    "safety": ["What safety conditions or requests require refusal or escalation?"],
    "coverage": ["Where do you work, and what geographical limits apply?"],
    "hours": ["When are you available, including exceptions and required notice?"],
}


def fingerprint(*items):
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def load(tenant):
    try:
        data = db.db().rpc("knowledge_review_context", {"p_tenant": tenant}).execute().data
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list) or not data.get("version"):
            raise ValueError()
        return data
    except Exception:
        raise HTTPException(503, "Knowledge and pending reviews could not be verified. Nothing was saved.") from None


def grounded_reply(context):
    """A review-only turn never fabricates replacement prose or changes cards."""
    pending=[r for r in context["reviews"] if r["state"]=="pending"]
    if not pending:
        return {"reply":"I can't verify an editable pending review for those cards. Open Knowledge and reread or capture the source before requesting corrections. No changes were made by this reply.",
                "link":{"label":"Open Knowledge","href":"/onboarding/knowledge"}}
    titles=[n["label"] for n in pending[0]["payload"]["entries"]]
    total=sum(len(r["payload"]["entries"]) for r in pending)
    text=(f"There are {total} stored pending drafts in {len(pending)} reviews, separate from saved Knowledge. "
          f"The linked review contains {len(titles)} drafts. Open it to check their text against the source statements, retain missing facts verbatim, and resolve overlaps. "
          "No changes were made by this reply. ")
    if titles:
        shown=titles[:8]
        text += "Stored titles in this review: "+"; ".join(shown)+". "
        if len(titles)>len(shown): text += f"The review contains {len(titles)-len(shown)} more draft titles. "
    text += "If you have unsaved form edits, use Check changes and overlaps before approving anything."
    return {"reply":text,"link":{"label":"Review actual pending Knowledge","href":"/onboarding/knowledge/review/"+pending[0]["id"]}}


def stage(tenant, entries, *, origin="Owner note", questions=None, mailbox_id=None, return_to="/onboarding/knowledge", source_units=None, evidence_pages=None):
    clean = []
    for n in entries or []:
        label, content = str(n.get("label") or "").strip(), str(n.get("content") or "").strip()
        if not label or not content:
            continue
        if len(label) > 160 or len(content) > 60000:
            raise HTTPException(400, "Split this source into smaller notes before reviewing it.")
        clean.append({"id": str(uuid4()), "label": label, "content": content,
                      "mailbox_id": mailbox_id, "provenance": n.get("provenance") or [{"source": origin}],
                      "topics": [k for k in n.get("topics", []) if k in QUESTIONS]})
    if len(clean) > 30 or sum(len(n["content"]) for n in clean) > 180000:
        raise HTTPException(400, "Review at most thirty notes at a time.")
    payload = {"entries": clean, "questions": clean_questions(questions), "origin": origin,
               "return_to": return_to, "topic_reviews": {}, "source_units":source_units or []}
    row = {"id": str(uuid4()), "tenant_id": tenant, "payload": payload, "state": "pending"}
    try:
        if evidence_pages is not None:
            from app import knowledge_source_evidence
            evidence = knowledge_source_evidence.build(evidence_pages)
            payload.update(source_snapshot=evidence['id'], draft_revision=1)
            result = db.db().rpc('capture_knowledge_review_sources', {
                'p_tenant': tenant, 'p_review': row['id'], 'p_payload': payload, 'p_evidence': evidence}).execute().data
            if result != row['id']:
                raise ValueError('Source capture was not confirmed')
        else:
            result = db.db().table("knowledge_reviews").insert(row).execute().data
    except Exception:
        raise HTTPException(503,"The drafts could not be confirmed. Nothing was admitted to Knowledge; reload before trying again.") from None
    if not result:
        raise HTTPException(503, "The drafts could not be stored. Nothing was admitted to Knowledge.")
    return row


def clean_questions(items):
    out = []
    for q in items or []:
        if isinstance(q, dict) and q.get("topic") in QUESTIONS and str(q.get("question") or "").strip():
            out.append({"topic": q["topic"], "question": str(q["question"]).strip()[:500]})
    return out[:24]


def topics(entry):
    return sorted(set(entry.get("topics") or coverage.classify(entry.get("label", ""), entry.get("content", ""))))


def saved_provenance(context, source):
    for r in reversed(context.get("reviews",[])):
        if r["state"]!="saved": continue
        for n in r["payload"]["entries"]:
            if (n.get("admitted_source"),n.get("mailbox_id"),n.get("admitted_content_hash")) == (
                    source["source"],source.get("mailbox_id"),fingerprint(source["content"])):
                return n.get("combined_provenance") or n["provenance"]
    return [{"source":source["source"]}]


def saved_source_context(context, source):
    for r in reversed(context.get("reviews", [])):
        if r["state"] != "saved": continue
        for n in r["payload"]["entries"]:
            if (n.get("admitted_source"), n.get("mailbox_id"), n.get("admitted_content_hash")) == (
                    source["source"], source.get("mailbox_id"), fingerprint(source["content"])):
                return n.get("source_context", "")
    return ""


def wording_segments(entry):
    """Keep legacy wording explicit without guessing who authored it."""
    return deepcopy(entry.get('content_segments') or [
        {'kind': 'unattributed', 'reference': 'Existing wording; authorship not recorded', 'text': entry['content']}])


def saved_wording_segments(context, source):
    for row in reversed(context.get('reviews', [])):
        if row['state'] != 'saved':
            continue
        for entry in row['payload']['entries']:
            if (entry.get('admitted_source'), entry.get('mailbox_id'), entry.get('admitted_content_hash')) == (
                    source['source'], source.get('mailbox_id'), fingerprint(source['content'])):
                return deepcopy(entry.get('combined_content_segments') or wording_segments(source))
    return wording_segments(source)


def candidates(entry, context, own_review=None, entries=None):
    targets = [{"id": "brief", "label": "Business brief", "content": context.get("brief", ""),
                "kind": "brief", "provenance": [{"source": "Saved business brief"}]}]
    targets += [{"id": "saved:" + fingerprint(n["source"], n.get("mailbox_id")), "label": n["source"],
                 "content": n["content"], "kind": "saved", "mailbox_id": n.get("mailbox_id"),
                 "provenance": saved_provenance(context,n),"source_context":saved_source_context(context,n),
                 "content_segments":saved_wording_segments(context,n)} for n in context["sources"]
                if n.get("mailbox_id") in (None, entry.get("mailbox_id"))]
    for review in context.get("reviews", []):
        if review["state"] != "pending":
            continue
        group = entries if review["id"] == own_review and entries is not None else review["payload"]["entries"]
        targets += [{**n, "kind": "pending"} for n in group if n["id"] != entry["id"]
                    and n.get("mailbox_id") in (None, entry.get("mailbox_id"))]
    found = []
    for target in targets:
        a, b = entry["content"], target["content"]
        stop = set("a an the and or of to for we our us you your is are be it its this that with in on at from by as have has will can do does".split())
        words_a, words_b = set(re.findall(r"\w+", a.lower()))-stop, set(re.findall(r"\w+", b.lower()))-stop
        common = words_a & words_b
        overlap = len(common) / max(1, min(len(words_a), len(words_b)))
        ratio = (SequenceMatcher(None, a.lower()[:1600], b.lower()[:1600]).ratio()
                 if len(common)>=5 and overlap>=.25 else 0)
        ga,gb = " ".join(re.findall(r"\w+",a.casefold()))," ".join(re.findall(r"\w+",b.casefold()))
        contained = min(len(ga),len(gb))>=12 and (ga in gb or gb in ga)
        same_label = entry["label"].casefold() == target["label"].removeprefix("Website — ").casefold()
        if b and (same_label or contained or a.strip().casefold() == b.strip().casefold() or
                  (len(common) >= 5 and (overlap >= .45 or ratio >= .65))):
            found.append({**target, "key": fingerprint(entry["id"], a, target["id"], b),
                          "overlap": sorted(common)[:24], "excerpt": b[:1200],
                          "reason": "Matching label" if same_label else "Shared wording; check meaning and numbers"})
    return found


def topic_view(context):
    metadata = {}
    for r in context.get("reviews",[]):
        if r["state"]=="saved":
            for n in r["payload"]["entries"]:
                if n.get("admitted_source"): metadata[(n["admitted_source"],n.get("mailbox_id"))]=n
    saved = []
    for s in context["sources"]:
        meta=metadata.get((s["source"],s.get("mailbox_id")),{})
        saved.append({"label":s["source"],"content":s["content"],"provenance":saved_provenance(context,s),
                      "topics":meta.get("combined_topics",meta.get("topics",[])) if meta.get("admitted_content_hash")==fingerprint(s["content"]) else []})
    pending = [n for r in context.get("reviews", []) if r["state"] == "pending" for n in r["payload"]["entries"]]
    reviewed = {}
    for r in context.get("reviews", []):
        if r["state"] == "saved": reviewed.update(r["payload"].get("topic_reviews", {}))
    result = []
    for k in coverage.KINDS:
        key = k["key"]
        have, draft = [s for s in saved if key in topics(s)], [s for s in pending if key in topics(s)]
        answers = reviewed.get(key, {})
        # Confirmations expire when any saved evidence changes.
        evidence = fingerprint([(s["source"],s.get("mailbox_id"),s["content"]) for s in sorted(context["sources"],key=lambda s:(s["source"],s.get("mailbox_id") or ""))])
        state = answers.get("state", "unknown") if answers.get("evidence") == evidence else ("partial" if have else "unknown")
        questions = [{"id": fingerprint(key, q), "question": q, "answer": "", "status": "unknown"} for q in QUESTIONS[key]]
        for r in context.get("reviews", []):
            if r["state"]=="discarded": continue
            for q in r["payload"].get("questions", []):
                if q["topic"] == key and q["question"] not in [v["question"] for v in questions]:
                    questions.append({"id": fingerprint(key,q["question"]), "question": q["question"], "answer": "", "status": "unknown"})
        old_answers = {q["id"]: q for q in answers.get("questions", [])}
        questions = [{**q, **old_answers.get(q["id"], {})} for q in questions]
        if state=="covered" and any(q["status"]=="unknown" for q in questions):
            state="partial" if have else "unknown"
        result.append({"key": key, "label": k["label"], "state": state, "saved": have, "pending": draft,
                       "questions": questions})
    return {"topics": result, "saved_count": sum(bool(t["saved"]) for t in result),
            "pending_count": sum(bool(t["pending"]) for t in result)}


def edited_entries(row, form, *, validate=True):
    entries = deepcopy(row["payload"]["entries"])
    for n in entries:
        original = n['content']
        n["label"] = str(form.get("label_"+n["id"],n["label"])).strip()
        n["content"] = str(form.get("content_"+n["id"],n["content"])).strip()
        if n['content'] != original:
            n['content_segments'] = [{'kind': 'owner_said', 'reference': 'Owner edited this Knowledge draft', 'text': n['content']}]
        if validate and form.get("action_"+n["id"])!="discard" and (not n["label"] or len(n["label"])>160 or not n["content"] or len(n["content"])>60000):
            raise HTTPException(400,"Supply a label within 160 characters and content within 60,000 characters.")
    return entries


def effective_entries(context, row, entries, form):
    effective = {n["id"]:deepcopy(n) for n in entries
                 if form.get("action_"+n["id"]) != "discard" and not str(form.get("action_"+n["id"],"")).startswith("merge_pending:")}
    for n in entries:
        action = str(form.get("action_"+n["id"],""))
        if action.startswith("merge_pending:"):
            target = effective.get(action.partition(":")[2])
            if not target or target["id"]==n["id"] or form.get("action_"+target["id"])!="keep":
                raise HTTPException(400,"Merge into another retained draft; merge chains are not allowed.")
            target['content_segments'] = wording_segments(target) + wording_segments(n)
            target["content"] += "\n\n" + n["content"]
            target["provenance"] += n["provenance"]
            target["topics"] = sorted(set(topics(target)+topics(n)))
            target["source_context"] = "\n".join(filter(None,[target.get("source_context"),n.get("source_context")]))
        elif action.startswith("merge_saved:"):
            match = next((m for m in candidates(n,context,row["id"],entries)
                          if m["kind"]=="saved" and m["id"]==action.removeprefix("merge_saved:")
                          and m.get("mailbox_id")==n.get("mailbox_id")),None)
            if not match: raise HTTPException(409,"That saved merge target is no longer a matching source. Check changes first.")
            effective[n["id"]].update(content=match["content"]+"\n\n"+n["content"], merge_target=match,
                provenance=match["provenance"]+n["provenance"],
                content_segments=wording_segments(match)+wording_segments(n),
                source_context="\n".join(filter(None,[match.get("source_context"),n.get("source_context")])))
    return list(effective.values())


def resolve_source_statements(row, entries, form, *, complete=False):
    from app import knowledge_fidelity
    before = {n['id']: n['content'] for n in entries if n.get('content_segments')}
    decisions = knowledge_fidelity.resolve(row, entries, form, complete=complete)
    for entry in entries:
        original = before.get(entry['id'])
        if original is not None and entry['content'] != original:
            # Fidelity appends exact statements to the chosen note. Preserve its
            # prior range attribution and identify this additional source text.
            entry['content_segments'].append({'kind': 'owner_site',
                'reference': 'Source-statement coverage review',
                'text': entry['content'][len(original):].strip()})
    return decisions


def review_view(context, row, form=None):
    row = deepcopy(row)
    form = dict(form or {})
    entries = edited_entries(row,form,validate=False)
    try: effective = effective_entries(context,row,entries,form)
    except HTTPException: effective = entries  # Preserve edits while the reviewer fixes an invalid choice.
    from app import knowledge_fidelity
    source_view=knowledge_fidelity.view(row,effective,form)
    try: resolve_source_statements(row,effective,form)
    except HTTPException: pass  # Keep invalid exclusions visible for correction.
    by_id = {n["id"]:n for n in effective}
    from app.config import settings
    return {**row, "source_review_enabled": settings.knowledge_source_review_enabled, "version": context["version"], "entries": [
        {**n, "combined":by_id.get(n["id"],n)["content"],"preview_provenance":by_id.get(n["id"],n)["provenance"],
         "content_segments":by_id.get(n["id"],n).get("content_segments",[]),
         "matches": [m for m in candidates(by_id[n["id"]],context,row["id"],effective)
                     if m["id"]!=by_id[n["id"]].get("merge_target",{}).get("id")] if n["id"] in by_id else [],
         "merge_options": [m for m in candidates(n,context,row["id"],entries)
                           if m["kind"]=="saved" and m.get("mailbox_id")==n.get("mailbox_id")]} for n in entries],
        "form": form, "coverage": topic_view(context), "source_view":source_view}


def plan(context, row, form):
    """Recompute candidates from edited drafts; every current match needs a choice."""
    if row["state"] != "pending" or form.get("version") != context["version"]:
        raise HTTPException(409, "Knowledge or pending drafts changed. Reload and review the latest evidence.")
    entries = edited_entries(row,form)
    if sum(len(n["content"]) for n in entries)>180000:
        raise HTTPException(400,"Split this review into smaller batches before saving.")
    for n in entries:
        action = form.get("action_"+n["id"], "")
        if action not in {"keep","discard"} and not str(action).startswith(("merge_pending:","merge_saved:")):
            raise HTTPException(400,"Choose whether to retain, merge or discard every draft.")
    effective = effective_entries(context,row,entries,form)
    source_decisions=resolve_source_statements(row,effective,form,complete=True)
    notes, outcomes = [], [{**n,"decision":form["action_"+n["id"]]} for n in entries]
    for n in effective:
        matches = candidates(n, context, row["id"], effective)
        for match in matches:
            if match["id"]==n.get("merge_target",{}).get("id"): continue
            if form.get("match_"+match["key"]) != "keep_both":
                raise HTTPException(409, "Resolve every overlap: keep both intentionally, or discard the draft. Nothing was saved.")
        # A fresh stable source name avoids an implicit replacement even on a label collision.
        source = n["merge_target"]["label"] if n.get("merge_target") else f"{n['label']} · {n['id'][:8]}"
        provenance = "\n".join("- " + p.get("source", "") + (" — " + p.get("excerpt", "") if p.get("excerpt") else "") for p in n["provenance"])
        notes.append({"label": source, "mailbox_id": n.get("mailbox_id"),
                      "content": n["content"] + "\n\nSource evidence:\n" + provenance,
                      "source_context":n.get("source_context","")})
        next(o for o in outcomes if o["id"]==n["id"]).update(matches=matches, admitted_source=source,
                                                          combined_provenance=n["provenance"],combined_content_segments=wording_segments(n),combined_topics=topics(n),source_context=n.get("source_context",""))
    topic_reviews = {}
    clarification = []
    for t in topic_view(context)["topics"]:
        state = form.get("topic_"+t["key"], "unknown")
        if state not in STATES: raise HTTPException(400, "Choose a valid topic state.")
        answers = []
        for q in t["questions"]:
            answer = str(form.get("answer_"+q["id"], "")).strip()
            if len(answer)>4000: raise HTTPException(400,"Keep each answer within 4,000 characters; split longer facts into notes.")
            status = form.get("question_"+q["id"], "unknown")
            if state=="not_applicable" and status=="unknown": status="not_applicable"
            if status not in {"unknown", "answered", "not_applicable"} or (status == "answered" and not answer):
                raise HTTPException(400, "An answered question needs the owner's actual answer.")
            if answer and status!="answered":
                raise HTTPException(400,"You typed an answer. Mark it answered, or clear it before leaving this detail unknown or not applicable.")
            answers.append({**q, "answer": answer if status == "answered" else "", "status": status})
            if status == "answered": clarification.append(q["question"] + "\nOwner answer: " + answer)
            elif status == "not_applicable": clarification.append(q["question"] + "\nOwner marked this question not applicable.")
        if state == "covered" and any(q["status"] == "unknown" for q in answers):
            raise HTTPException(400, "Answer the open questions or mark them not applicable before confirming covered.")
        if state == "not_applicable": clarification.append(t["label"] + ": owner explicitly marked this topic not applicable.")
        topic_reviews[t["key"]] = {"state": state, "questions": answers}
    if clarification:
        notes.append({"label": "Owner clarifications · " + row["id"][:8], "content": "\n\n".join(clarification), "mailbox_id": None})
    return notes, {**row["payload"], "original_entries": deepcopy(row["payload"]["entries"]),
                   "entries": outcomes, "topic_reviews": topic_reviews,"source_decisions":source_decisions}


async def admit(tenant, row, context, form, *, reviewer=None):
    notes, payload = plan(context, row, form)
    payload.update(reviewed_by=reviewer, reviewed_at=datetime.now(timezone.utc).isoformat())
    from app import setup_card_knowledge, usage
    prepared, tokens = await setup_card_knowledge.prepare({"notes": notes}) if notes else ({"notes": []}, 0)
    if notes:
        from app.config import settings
        usage.record_voyage(tenant,settings.voyage_model,tokens,operation="embed_ingest")
    # Confirmations bind to the resulting saved evidence; the SQL transaction rechecks the input snapshot.
    identities = {(n["label"],n.get("mailbox_id")) for n in notes}
    if len(identities)!=len(notes): raise HTTPException(400,"Merge into each saved source only once per review.")
    resulting = [s for s in context["sources"] if (s["source"],s.get("mailbox_id")) not in identities]
    resulting += [{"source":n["label"],"mailbox_id":n.get("mailbox_id"),"content":"\n\n".join(c["content"] for c in n["chunks"])} for n in prepared["notes"]]
    for n in payload["entries"]:
        saved_note=next((s for s in resulting if s["source"]==n.get("admitted_source") and s.get("mailbox_id")==n.get("mailbox_id")),None)
        if saved_note: n["admitted_content_hash"]=fingerprint(saved_note["content"])
    evidence = fingerprint([(s["source"],s.get("mailbox_id"),s["content"]) for s in sorted(resulting,key=lambda s:(s["source"],s.get("mailbox_id") or ""))])
    for t in payload["topic_reviews"].values(): t["evidence"] = evidence
    try:
        result = db.db().rpc("admit_knowledge_review", {"p_tenant": tenant, "p_review": row["id"],
            "p_expected": context["version"], "p_payload": payload, "p_notes": prepared["notes"]}).execute().data
        if result is not True: raise RuntimeError()
    except Exception as exc:
        raise HTTPException(409, "Knowledge changed or the save could not be confirmed. Reload before retrying.") from exc
    return len(notes)
