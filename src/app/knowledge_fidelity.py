"""Conservative source coverage, independent of the business model or an LLM summary.

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
    typography = str.maketrans({'’':"'", '‘':"'", '“':'"', '”':'"', '\u00a0':' '})
    text, content = normal(text.translate(typography)), normal(content.translate(typography))
    if text in content:
        return True
    # A complete sentence may be retained as a clause joined with a comma.
    # Match its exact words and a real clause boundary, never a loose substring.
    if text.endswith('.') and re.search(r'(?<!\w)'+re.escape(text[:-1])+r'(?=[,;.!?])', content):
        return True
    if re.match(r'^(Our|We|The|Your|You|It|This|These|That|Those)\b', text):
        return text[0].lower() + text[1:] in content
    return False


def fact_sequence(text):
    """Conservative lexical evidence, not a proof of semantic equivalence.

    Ignore sentence framing only. Numbers, names, units, negation and scope
    remain mandatory. Unrecognised paraphrases need an explicit owner review.
    """
    # Prepositions and conjunctions carry relationships, direction and ranges.
    # They are facts, even when the same names/numbers occur on both sides.
    framing = set('a an the we our you your it its this these that those is are be'.split())
    aliases = {'reports': 'report', 'reporting': 'report', 'repairs': 'repair',
               'properties': 'property', 'invoices': 'invoice', 'photos': 'photo',
               'hours': 'hour', 'fees': 'fee', 'prices': 'price'}
    return [aliases.get(word, word) for word in re.findall(r"\w+|[£$€%]", text.casefold()) if word not in framing]


def fact_tokens(text):
    return set(fact_sequence(text))


def represented_fact(text, content):
    if _represented(text, content):
        return True
    sensitive = re.findall(r'\b(?:\w*[a-z][A-Z]\w*|[A-Z]{2,})\b',text)
    if any(not re.search(r'\b'+re.escape(word)+r'\b',content) for word in sensitive):return False
    tokens = fact_tokens(text)
    # Match a bounded clause, never a bag of words across unrelated notes.
    # The same vocabulary can express opposite facts: negation must agree.
    negatives = {'no', 'not', 'never', 'without', 'cannot'}
    # Extra words can change scope too ("Monday and Friday" versus "Monday
    # to Thursday and Friday"). A subsequence is not evidence of equivalence.
    sequence = fact_sequence(text)
    return bool(tokens) and any(sequence == fact_sequence(clause)
        and tokens & negatives == fact_tokens(clause) & negatives
        for clause in re.split(r'\n\n|(?<=[.!?])\s+', content))


def critical_terms(text):
    words = re.findall(r"\w+|[£$€%]",text)
    skip={'Our','We','The','Your','You','It','This','These','That','Those','A','An',
          'Include','Mention','Use','Supply','Return','Also','Please','Note','Choose','Keep','Ensure',
          'Check','Report','Submit','Send','Provide','See','Visit','Request','Review'}
    protected=set('no not never cannot only unless except if before after until excluding without per day days week weeks month months year years minute minutes hour hours am pm free including'.split())
    return {w.casefold() for w in words if any(c.isdigit() for c in w) or w in {'£','$','€','%'}
            or w[0].isupper() and w not in skip or w.casefold() in protected}


def layout_fragment(unit):
    """Suggest short structural labels for grouped owner review, never approval."""
    places = {p for span in unit.get('spans', []) for p in span.get('locations', [])}
    text = unit['text']
    protected = re.search(r"\d|[£$€@]|\b(no|not|never|only|unless|except|if|before|after|until|excluding|without|includes?|is|are|we|our|must|can|will|available|closed|open|free)\b", text, re.I)
    labels = {'home','menu','navigation','contact','contact us','about','about us','services',
              'what we do','service area','prices','fees','fees and prices','get started',
              'learn more','read more','privacy','privacy policy','terms','back','next',
              'reporting a repair','ongoing service','starting management','hours and contact'}
    return (bool(places & {'nav','h1','h2','h3','h4'}) and all(part.strip().casefold() in labels for part in text.split('|'))
            and not protected and unit['kind']=='statement')



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
    """Link factual evidence without adding quotations to readable Knowledge.

    Conservative matches or an explicit owner meaning decision account for facts;
    unresolved material gaps stay visible. Exclusions need an owner reason.
    """
    units = row["payload"].get("source_units", [])
    by_id = {n["id"]:n for n in entries}
    prefix = qualification_context(units) if entries else ""
    decisions = []
    for unit in units:
        if not entries:
            decisions.append({**unit, "status":"discarded_with_drafts", "entries":[]})
            continue
        represented = [n["id"] for n in entries if represented_fact(unit["text"], n["content"])]
        choice = str(form.get("source_"+unit["id"], ""))
        reason = str(form.get("source_reason_"+unit["id"], "")).strip()
        if unit['kind'] != 'qualification' and represented and choice in {'exclude','unknown'}:
            if complete:
                raise HTTPException(409,'Excluded or unknown source wording is still in a retained note. Edit that note before saving.')
            decisions.append({**unit,'status':'unresolved','entries':represented,
                              'conflict':'This wording is still in a retained note.'})
            continue
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
            for identity in represented:
                for url in unit['sources']:
                    citation={'source':url,'excerpt':unit['text']}
                    if citation not in by_id[identity]['provenance']:by_id[identity]['provenance'].append(citation)
            decision={"status":"literal_in_note","entries":represented}
        elif choice.startswith('covered:'):
            target=by_id.get(choice.removeprefix('covered:'))
            if not target or not critical_terms(unit['text']) <= {w.casefold() for w in re.findall(r"\w+|[£$€%]",target['content'])}:
                if complete:raise HTTPException(409,'The paraphrase decision is missing a critical name, number, unit or condition. Correct the affected note before Save.')
                decision={'status':'unresolved','entries':[]}
            else:
                target['provenance'] += [{'source':url,'excerpt':unit['text'],'review':'owner_confirmed_meaning'} for url in unit['sources']]
                decision={'status':'owner_confirmed_meaning','entries':[target['id']]}
        elif choice == 'unknown':
            decision={"status":"deferred_unknown","entries":[]}
        elif not choice and layout_fragment(unit):
            reviewed = True
            decision={"status":"layout_fragment","entries":[],"reviewed":reviewed}
        else:
            choice = choice or str(form.get("source_default", ""))
            target = by_id.get(choice.removeprefix("note:")) if choice.startswith("note:") else None
            if target:
                if not represented_fact(unit['text'], target['content']):
                    if complete:
                        raise HTTPException(409, 'A retained source fact is not verified in '+target['label']+'. Add the missing fact to that note, or explain the source decision. The original evidence remains in the audit.')
                    decisions.append({**unit, 'status':'unresolved', 'entries':[target['id']]})
                    continue
                target["provenance"] += [{"source":url,"excerpt":unit["text"]} for url in unit["sources"]]
                decision={"status":"retained_evidence","entries":[target["id"]]}
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
    return {"units":units,"unresolved":sum(u["status"]=="unresolved" or (u['status']=='layout_fragment' and not u.get('reviewed')) for u in units),
            "qualifications":[u for u in units if u["kind"]=="qualification"]}
