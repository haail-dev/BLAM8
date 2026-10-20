"""Exact source coverage, independent of the business model or an LLM summary.

This inventories the text actually read, not the whole website. Labels prioritise
review; they do not decide which business rules are true or applicable.
"""
import hashlib
import re
from fastapi import HTTPException

QUALIFICATION = re.compile(r"\b(fictional|illustrative|synthetic|sample|demonstration|provisional)\b|\btest[ -](only|rig|profile|facts|data|business|site|website|guidance)\b|\bnot a (live|real)\b|\bno real\b", re.I)
CONSTRAINT = re.compile(r"\b(must|never|cannot|do not|does not|shall|should not|only if|unless|not until)\b", re.I)


def normal(text):
    return " ".join(text.split())


def _represented(text, content):
    """Allow a sentence opener to be embedded mid-sentence, preserving facts/codes."""
    text, content = normal(text), normal(content)
    if text in content:
        return True
    if re.match(r'^(Our|We|The|Your|You|It|This|These|That|Those)\b', text):
        return text[0].lower() + text[1:] in content
    return False


def inventory(pages):
    """Keep every read statement; repeated wording shares an ID and all sources."""
    units = {}
    for page in pages:
        # Preserve newlines and sentence boundaries without rewriting any words.
        offset = 0
        for paragraph in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z‘“\"'])", page["text"]):
            text = normal(paragraph)
            if not text:
                continue
            start = page['text'].find(paragraph, offset)
            end = start + len(paragraph)
            offset = end
            locations = sorted({b['location'] for b in page.get('blocks', []) if b['start'] < end and b['end'] > start})
            key = hashlib.sha256(text.encode()).hexdigest()[:24]
            row = units.setdefault(key, {"id":key,"text":text,"sources":[],
                "kind":"qualification" if QUALIFICATION.search(text) else "constraint" if CONSTRAINT.search(text) else "statement"})
            if page["url"] not in row["sources"]:
                row["sources"].append(page["url"])
            row.setdefault('spans', []).append({'url': page['url'], 'start': start, 'end': end,
                                                'locations': locations or ['body']})
    if len(units)>1000:
        raise ValueError("The source has too many statements for one review. Import smaller sections; no statements were silently dropped.")
    return list(units.values())


def qualification_context(units):
    return "\n".join(f"Source qualification ({', '.join(u['sources'])}): {u['text']}"
                     for u in units if u["kind"]=="qualification")


def resolve(row, entries, form, *, complete=False):
    """Attach exact missing statements only to an explicitly retained target.

    Literal presence in approved text is verifiable; a citation or paraphrase
    alone is not proof of coverage. Exclusion always needs a reviewer reason.
    """
    units = row["payload"].get("source_units", [])
    by_id = {n["id"]:n for n in entries}
    prefix = qualification_context(units) if entries else ""
    decisions = []
    for unit in units:
        if not entries:
            decisions.append({**unit, "status":"discarded_with_drafts", "entries":[]})
            continue
        represented = [n["id"] for n in entries if _represented(unit["text"], n["content"])]
        choice = str(form.get("source_"+unit["id"], ""))
        reason = str(form.get("source_reason_"+unit["id"], "")).strip()
        if unit["kind"]=="qualification" and entries:
            if choice=="exclude":
                raise HTTPException(409,"Source qualifications must remain attached to source-derived facts. Nothing was saved.")
            decision = {"status":"retained_context","entries":list(by_id)}
        elif choice=="exclude":
            if not reason:
                if complete: raise HTTPException(400,"Explain each excluded source statement; absence is not a decision.")
                decision={"status":"unresolved","entries":[]}
            else:
                if len(reason)>1000:
                    raise HTTPException(400,"Keep each exclusion reason within 1,000 characters.")
                decision={"status":"excluded","entries":[],"reason":reason}
        elif represented and not choice:
            decision={"status":"literal_in_note","entries":represented}
        else:
            choice = choice or str(form.get("source_default", ""))
            target = by_id.get(choice.removeprefix("note:")) if choice.startswith("note:") else None
            if target:
                if not _represented(unit["text"], target["content"]):
                    target["content"] += "\n\nRetained source statement ("+", ".join(unit["sources"])+"): " + unit["text"]
                target["provenance"] += [{"source":url,"excerpt":unit["text"]} for url in unit["sources"]]
                decision={"status":"retained_verbatim","entries":[target["id"]]}
            else:
                if complete: raise HTTPException(409,"Some source statements are unaccounted for. Retain them in a kept note or explicitly exclude them with a reason.")
                decision={"status":"unresolved","entries":[]}
        decisions.append({**unit, **decision})
    for entry in entries:
        if prefix:
            entry["source_context"] = "\n".join(dict.fromkeys(filter(None,
                entry.get("source_context", "").splitlines()+prefix.splitlines())))
    return decisions


def view(row, entries, form):
    from copy import deepcopy
    copies=deepcopy(entries)
    try:
        units=resolve(row,copies,form)
    except HTTPException:
        units=[{**u,"status":"unresolved","entries":[]} for u in row["payload"].get("source_units",[])]
    for unit in units:
        unit["suggested"]=[n["label"] for n in entries if any(
            p["source"] in unit["sources"] for p in n["provenance"])]
    return {"units":units,"unresolved":sum(u["status"]=="unresolved" for u in units),
            "qualifications":[u for u in units if u["kind"]=="qualification"]}
