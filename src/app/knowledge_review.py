"""Shared, explicit knowledge admission. Suggestions never become saved facts here.

Overlap scores are review hints, not semantic equivalence. An absent answer is
unknown; only a reviewer can confirm coverage or mark a topic not applicable.
"""
from copy import deepcopy
from datetime import datetime, timezone
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
          f"The linked review contains {len(titles)} drafts. Open it to review the facts, correct any material gaps and resolve overlaps. Source quotations stay in the optional audit. "
          "No changes were made by this reply. ")
    if titles:
        shown=titles[:8]
        text += "Stored titles in this review: "+"; ".join(shown)+". "
        if len(titles)>len(shown): text += f"The review contains {len(titles)-len(shown)} more draft titles. "
    text += "If you have unsaved form edits, use Check changes and overlaps before approving anything."
    return {"reply":text,"link":{"label":"Review actual pending Knowledge","href":"/onboarding/knowledge/review/"+pending[0]["id"]}}


def website_state(context):
    pending = [r for r in context['reviews'] if r['state']=='pending' and r['payload'].get('origin','').startswith('Website')]
    saved_names = {n.get('admitted_source') for r in context['reviews'] if r['state']=='saved'
                   and r['payload'].get('origin','').startswith('Website') for n in r['payload']['entries']}
    saved = [s for s in context['sources'] if s['source'] in saved_names or s['source'].startswith('Website — ')]
    return {'state': 'pending' if pending else 'saved' if saved else 'idle',
            'pending_notes': sum(len(r['payload']['entries']) for r in pending), 'saved_notes': len(saved),
            'clarification_notes':sum(s['source'].startswith('Owner clarifications · ') for s in context['sources']),
            'review_href': '#review-'+pending[0]['id'] if pending else None}


def stage(tenant, entries, *, origin="Owner note", questions=None, mailbox_id=None, return_to="/onboarding/knowledge", source_units=None, evidence_pages=None, capture_manifest=None, authority_context=None):
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
               "return_to": return_to, "topic_reviews": {}, "source_units":source_units or [],
               "capture_manifest": capture_manifest or []}
    if authority_context:
        preserve_owner_corrections(payload, authority_context)
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


def preserve_owner_corrections(payload, context):
    """Reapply exact saved owner replacements on a reread of the same source.

    Source/correction history stays in audit. A different source or ambiguous
    occurrence is never rewritten heuristically. Admission separately rejects
    unresolved stale facts so they cannot re-enter active retrieval.
    """
    from app import knowledge_fidelity
    authority=[]
    for review in context.get('reviews',[]):
        if review['state']!='saved':continue
        saved_entries={n['id']:n for n in review['payload']['entries']}
        for history in review['payload'].get('source_review_history',[]):
            for change in history.get('changes',[]):
                original=saved_entries.get(change['id'],{})
                urls={p['source'] for p in original.get('provenance',[]) if isinstance(p,dict) and isinstance(p.get('source'),str)}
                for edit in change.get('owner_replacements',[]):
                    if not isinstance(edit,dict) or not all(isinstance(edit.get(k),str) and edit[k] for k in ('before','after')):continue
                    authority.append({'label':original.get('label'), 'sources':sorted(urls),
                        'before':edit['before'],'after':edit['after'], 'request':history.get('owner_request','Owner correction'),
                        'claims':[c['text'] for c in change.get('source_choices',[]) if c.get('action')=='superseded'] or [change.get('before') or original.get('content','')]})
    payload['owner_authority']=authority
    choices=payload.setdefault('draft_review_choices',{})
    for rule in authority:
        old_units=[unit for unit in payload.get('source_units',[]) if rule['before'] in unit['text']
            and set(unit['sources']).intersection(rule['sources']) and any(
                knowledge_fidelity._represented(unit['text'],claim) or knowledge_fidelity._represented(claim,unit['text'])
                for claim in rule['claims'] if claim)]
        for unit in old_units:
            choices['source_'+unit['id']]='exclude'
            choices['source_reason_'+unit['id']]='Superseded by saved owner correction: '+rule['request'][:900]
        for note in payload['entries']:
            urls={p['source'] for p in note.get('provenance',[]) if isinstance(p,dict) and isinstance(p.get('source'),str)}
            if not urls.intersection(rule['sources']):continue
            scoped=(note['label']==rule['label'] or any(knowledge_fidelity._represented(u['text'],note['content']) for u in old_units)
                    or any(knowledge_fidelity._represented(claim,note['content']) for claim in rule['claims'] if claim))
            if scoped and note['content'].count(rule['before'])==1:
                note['content']=note['content'].replace(rule['before'],rule['after'])
                note['content_segments']=[{'kind':'owner_said','reference':rule['request'],'text':note['content']}]


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


def shared_passages(a, b):
    """Exact full-content sentence/block repetitions, not semantic equivalence."""
    def passages(text):
        return {' '.join(p.split()).casefold(): p.strip() for p in re.split(r'\n+|(?<=[.!?])\s+', text)
                if len(p.strip()) >= 40 and len(p.split()) >= 7}
    left, right = passages(a), passages(b)
    return [right[k] for k in right if k in left]


def candidates(entry, context, own_review=None, entries=None, *, include_merge_targets=False):
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
        from app.knowledge_comparison import comparison
        comparisons = comparison(a, b)
        same_saved_label = (target['kind'] == 'saved' and entry['label'].casefold() ==
                            target['label'].removeprefix('Website — ').casefold())
        if not comparisons and not (include_merge_targets and same_saved_label):
            continue
        passages = shared_passages(a, b)
        difference = any(p['type'] == 'difference' for p in comparisons)
        pair = sorted([(entry['id'], a), (target['id'], b)])
        found.append({**target, "key": fingerprint(pair),
                      "overlap": sorted(set(re.findall(r"\w+", a.lower())) & set(re.findall(r"\w+", b.lower())))[:24],
                      "excerpt": b[:1200], 'exact_passages': passages, 'comparisons': comparisons,
                      'comparison_title': 'Check a possible fact difference' if difference else 'Repeated text',
                      'source_labels': list(dict.fromkeys(p.get('source', '') for p in target.get('provenance', []) if p.get('source'))),
                      "reason": "Similar statements contain different numbers or conditions. Check whether their scope differs or one needs correcting." if difference else
                          "The quoted wording appears in both places. Keep both if each note needs it, or edit or merge the repeated text."})
    return found


def topic_evidence(key, sources):
    return fingerprint([(n['source'],n.get('mailbox_id'),n['content'])
        for n in sorted(sources,key=lambda n:(n['source'],n.get('mailbox_id') or ''))
        if key in coverage.classify(n['source'],n['content'])])


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
        current = (answers['scoped_evidence'] == topic_evidence(key,context['sources'])
                   if 'scoped_evidence' in answers else answers.get('evidence') == evidence)
        state = answers.get("state", "unknown") if current else ("partial" if have else "unknown")
        # A default interview is useful only when no source evidence exists.
        # Mentioned topics remain unknown/partial; this never certifies coverage.
        defaults = QUESTIONS[key] if not (have or draft) else []
        questions = [{"id": fingerprint(key, q), "question": q, "answer": "", "status": "unknown"} for q in defaults]
        for q in answers.get('questions', []):
            if q['id'] not in {v['id'] for v in questions}:
                questions.append(q)
        for r in context.get("reviews", []):
            if r["state"]=="discarded": continue
            for q in r["payload"].get("questions", []):
                if q["topic"] == key and q["question"] not in [v["question"] for v in questions]:
                    questions.append({"id": fingerprint(key,q["question"]), "question": q["question"], "answer": "", "status": "unknown"})
        if key == 'cadence' and any(re.search(r'\binspection\w*\b.*\bagreement\b|\bagreement\b.*\binspection\w*\b', n['content'],re.I) for n in have+draft):
            questions = [q for q in questions if not re.search(r'\binspection\w*\b',q['question'],re.I) or not re.search(r'frequen|how often|interval',q['question'],re.I)]
        old_answers = {q["id"]: q for q in answers.get("questions", [])}
        questions = [{**q, **old_answers.get(q["id"], {})} for q in questions]
        if state=="covered" and any(q["status"]=="unknown" for q in questions):
            state="partial" if have else "unknown"
        result.append({"key": key, "label": k["label"], "state": state, "saved": have, "pending": draft,
                       "questions": questions})
    return {"topics": result, "saved_count": sum(bool(t["saved"]) for t in result),
            "pending_count": sum(bool(t["pending"]) for t in result)}


def normalise_text(value):
    """Line endings carry no review meaning, but they change every hash of the text.

    A native form submission sends textarea newlines as CRLF; the Assistant guide
    and device sync post the same field from textarea.value with LF. Fingerprint
    and compare one form so a choice made on either path holds on the other.
    """
    return value.replace('\r\n', '\n').replace('\r', '\n') if isinstance(value, str) else value


def review_form(row, form=None):
    """Recover explicitly applied draft review choices, never inferred approvals."""
    merged = {**row['payload'].get('draft_review_choices', {}), **dict(form or {})}
    incoming=dict(form or {})
    merged = {name: normalise_text(value) for name, value in merged.items()}
    for n in row['payload']['entries']:
        key = 'reviewed_'+n['id']
        revision = fingerprint(merged.get('label_'+n['id'], n['label']), merged.get('content_'+n['id'], n['content']))
        if merged.get(key) and merged[key] != revision:
            merged['action_'+n['id']] = ''
            merged[key] = ''
    for unit in row['payload'].get('source_units',[]):
        stamp = merged.get('source_revision_'+unit['id'])
        choice='source_'+unit['id']
        if choice in incoming and incoming[choice] != row['payload'].get('draft_review_choices',{}).get(choice,''):
            merged['source_revision_'+unit['id']]=''
            continue
        if stamp and stamp != source_revision(row, unit, merged):
            merged['source_'+unit['id']] = ''
            merged['source_reason_'+unit['id']] = ''
            merged['source_revision_'+unit['id']] = ''
    return merged


def source_revision(row, unit, form):
    from app import knowledge_guidance
    targets = knowledge_guidance.source_targets(unit, row['payload']['entries'])
    explicit = form.get('source_'+unit['id'],'').partition(':')[2]
    ids = {n['id'] for n in targets} | {explicit}
    return fingerprint(unit, [(n['id'], form.get('content_'+n['id'], n['content']))
        for n in row['payload']['entries'] if n['id'] in ids])


def checkpoint(tenant, context, row, form):
    """CAS once; a competing write never causes an automatic retry."""
    if form.get('version') != context['version']:
        raise HTTPException(409,'The review changed. Compare your local edits with the stored review before continuing.')
    choices = {k:v for k,v in review_form(row,form).items() if k not in {'version','csrf_token','operation'}}
    for n in row['payload']['entries']:
        if choices.get('action_'+n['id']):
            choices['reviewed_'+n['id']] = fingerprint(choices.get('label_'+n['id'],n['label']), choices.get('content_'+n['id'],n['content']))
    for unit in row['payload'].get('source_units',[]):
        if choices.get('source_'+unit['id']):
            choices['source_revision_'+unit['id']] = source_revision(row,unit,choices)
    if choices == row['payload'].get('draft_review_choices',{}):
        return context, row, {**choices, 'version':context['version']}
    try:
        fresh = db.db().rpc('checkpoint_knowledge_review', {'p_tenant':tenant,'p_review':row['id'],
            'p_expected':context['version'],'p_choices':choices}).execute().data
        if not isinstance(fresh,dict) or not fresh.get('version'):raise ValueError()
    except Exception:
        raise HTTPException(409,'Draft review choices could not be confirmed. Your local edits remain; reload to compare before retrying.') from None
    stored = next(r for r in fresh['reviews'] if r['id']==row['id'])
    return fresh, stored, {**choices, 'version':fresh['version']}



def edited_entries(row, form, *, validate=True):
    entries = deepcopy(row["payload"]["entries"])
    for n in entries:
        original = normalise_text(n['content'])
        n["label"] = normalise_text(str(form.get("label_"+n["id"],n["label"]))).strip()
        n["content"] = normalise_text(str(form.get("content_"+n["id"],n["content"]))).strip()
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
            match = next((m for m in candidates(n,context,row["id"],entries, include_merge_targets=True)
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
    form = review_form(row, form)
    entries = edited_entries(row,form,validate=False)
    try: effective = effective_entries(context,row,entries,form)
    except HTTPException: effective = entries  # Preserve edits while the reviewer fixes an invalid choice.
    from app import knowledge_fidelity
    source_view=knowledge_fidelity.view(row,effective,form)
    try: resolve_source_statements(row,effective,form)
    except HTTPException: pass  # Keep invalid exclusions visible for correction.
    by_id = {n["id"]:n for n in effective}
    from app.config import settings
    result = {**row, "source_review_enabled": settings.knowledge_source_review_enabled, "version": context["version"], "entries": [
        {**n, "combined":by_id.get(n["id"],n)["content"],"preview_provenance":by_id.get(n["id"],n)["provenance"],
         "content_segments":by_id.get(n["id"],n).get("content_segments",[]),
         "matches": [m for m in candidates(by_id[n["id"]],context,row["id"],effective)
                     if m["id"]!=by_id[n["id"]].get("merge_target",{}).get("id")] if n["id"] in by_id else [],
         "merge_options": [m for m in candidates(n,context,row["id"],entries, include_merge_targets=True)
                           if m["kind"]=="saved" and m.get("mailbox_id")==n.get("mailbox_id")]} for n in entries],
        "form": form, "coverage": topic_view(context), "source_view":source_view}
    seen = set()
    for entry in result['entries']:
        unique = []
        for match in entry['matches']:
            if match['key'] not in seen:
                seen.add(match['key'])
                unique.append(match)
        entry['matches'] = unique
    result['signatures'] = {'content': fingerprint(row['payload']['entries'], row['payload'].get('draft_revision')),
                            'evidence': fingerprint(row['payload'].get('source_units'), row['payload'].get('source_snapshot'), row['payload'].get('capture_manifest')),
                            'context': context['version']}
    result['summary'] = {'notes': len(entries), 'decided': sum(bool(form.get('action_'+n['id'])) for n in entries),
                         'overlaps': len(seen), 'unresolved': source_view['unresolved'],
                         'questions': sum(q['status']=='unknown' for t in result['coverage']['topics'] for q in t['questions'])}
    return result


def notes_only_sources(row, entries, form):
    """Defer untouched extra evidence under the visible notes-only Save scope.

    Never approve evidence or repair an explicit invalid decision. Represented
    facts and shared qualifications remain subject to the normal fidelity rules.
    """
    result = dict(form)
    if form.get('review_scope') != 'notes' or form.get('source_default'):
        return result
    from app import knowledge_fidelity
    for unit in knowledge_fidelity.view(row, entries, form)['units']:
        key = 'source_' + unit['id']
        if (unit['status'] == 'unresolved' and not unit.get('entries')
                and unit['kind'] != 'qualification' and not form.get(key)):
            result[key] = 'unknown'
    return result


def plan(context, row, form):
    """Recompute candidates from edited drafts; every current match needs a choice."""
    form = review_form(row, form)
    if row["state"] != "pending" or form.get("version") != context["version"]:
        raise HTTPException(409, "Knowledge or pending drafts changed. Reload and review the latest evidence.")
    entries = edited_entries(row,form)
    for rule in row['payload'].get('owner_authority',[]):
        if any(rule['before'] in n['content'].replace(rule['after'],'')
               and {p.get('source') for p in n.get('provenance',[])}.intersection(rule['sources']) for n in entries):
            raise HTTPException(409,'This source still contains a previously corrected value. Its scope needs review before final Save; keep the saved owner correction authoritative: '+rule['after'])
    if sum(len(n["content"]) for n in entries)>180000:
        raise HTTPException(400,"Split this review into smaller batches before saving.")
    for n in entries:
        action = form.get("action_"+n["id"], "")
        if action not in {"keep","discard"} and not str(action).startswith(("merge_pending:","merge_saved:")):
            raise HTTPException(400,"Choose whether to retain, merge or discard every draft.")
    effective = effective_entries(context,row,entries,form)
    form = notes_only_sources(row, effective, form)
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
        # Evidence is an audit of the source we read, not a second set of
        # approved facts. Repeating excerpts here resurrects excluded/superseded
        # wording inside embeddings and every retrieval consumer. Keep the
        # complete quotations in the saved review payload, outside grounding.
        provenance = "\n".join(dict.fromkeys("- " + p.get("source", "") for p in n["provenance"]))
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
            prior = next((v for v in t["questions"] if v["id"]==q["id"]), {})
            if status == "answered" and (prior.get("answer") != answer or prior.get("status") != status): clarification.append(q["question"] + "\nOwner answer: " + answer)
            elif status == "not_applicable" and prior.get("status") != status: clarification.append(q["question"] + "\nOwner marked this question not applicable.")
        if state == "covered" and any(q["status"] == "unknown" for q in answers):
            raise HTTPException(400, "Answer the open questions or mark them not applicable before confirming covered.")
        if state == "not_applicable": clarification.append(t["label"] + ": owner explicitly marked this topic not applicable.")
        topic_reviews[t["key"]] = {"state": state, "questions": answers}
    if clarification:
        notes.append({"label": "Owner clarifications · " + row["id"][:8], "content": "\n\n".join(clarification), "mailbox_id": None})
    return notes, {**row["payload"], "original_entries": deepcopy(row["payload"]["entries"]),
                   "entries": outcomes, "topic_reviews": topic_reviews,"source_decisions":source_decisions,
                   "deferred_questions": {q['id']:{**q,'status':q['status'] if q['status'] in {'answered','not_applicable'} else 'deferred',
                       'destination':{'prices':'charges.terms','payment':'charges.terms','cadence':'diary.time','rules':'diary.conditions',
                           'process':'diary.conditions','safety':'diary.activities','coverage':'diary.time','hours':'diary.time'}[topic]}
                       for topic,t in topic_reviews.items() for q in t['questions']
                       if any(v['question']==q['question'] and v['topic']==topic for v in row['payload'].get('questions',[]))}}


async def admit(tenant, row, context, form, *, reviewer=None):
    try:
        notes, payload = plan(context, row, form)
    except HTTPException as exc:
        # A current draft can fail content validation without a device conflict.
        # Persisted-state/CAS failures below must still require reconciliation.
        if row['state']=='pending' and form.get('version')==context['version']:
            exc.headers={**(exc.headers or {}),'X-Blam8-Review-Validation':'1'}
        raise
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
    for key,t in payload["topic_reviews"].items():
        t['evidence'] = evidence  # legacy consumers
        t['scoped_evidence'] = topic_evidence(key,resulting)
    try:
        result = db.db().rpc("admit_knowledge_review", {"p_tenant": tenant, "p_review": row["id"],
            "p_expected": context["version"], "p_payload": payload, "p_notes": prepared["notes"]}).execute().data
        if result is not True: raise RuntimeError()
    except Exception as exc:
        raise HTTPException(409, "Knowledge changed or the save could not be confirmed. Reload before retrying.") from exc
    return len(notes)
