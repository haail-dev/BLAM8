"""Knowledge coverage — what the agent can answer, and what it still can't.

Every e2e run seeded the same eight kinds of note before the agent could
hold its own: the prices it must never defer, the rules it enforces, the
process a new customer goes through, the cadence work repeats on, how
people pay, what it refuses, where it works, and when it's open. The
knowledge base is a pile of notes; this reads the pile back as that
checklist, so the owner can see which kinds are covered and the assistant
can ask for exactly the ones that aren't (Kris 2026-09-04: "get the chat
prompting for certain infos related to their business type").

Pure and cheap: a keyword read over each source's label and opening text.
It is a coverage HINT, not a judge — a note that mentions £ counts as
prices whether or not every price is in it. The recommend card that
saves notes speaks the same "Label | content" as the settings cards."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from app.knowledge_note_structure import SUBJECT_NOTES

# (key, label, what the agent needs it for, the question to ask, keywords)
KINDS: list[dict] = [
    {"key": "prices", "label": "Prices",
     "why": "quoted in the reply, never deferred to a colleague",
     "ask": "What do you charge — your main services or products, with the prices the agent may quote, and anything that changes the price (quantity, distance, out-of-hours)?",
     "words": r"£|\$|€|\bprice|\bprices|\bcost|\bcharge|\brate\b|\brates\b|per hour|per day|per night|\bfee\b|\bquote"},
    {"key": "rules", "label": "Rules and judgement",
     "why": "the things it must check or refuse before it books",
     "ask": "Is there anything you won't do, or won't do until something else has happened first — a referral, a check, a certificate, an age or size limit?",
     "words": r"\bnever\b|\bwe don'?t\b|\bwe do not\b|\bmust\b|\bonly if\b|\bunless\b|\brefer|\brule\b|\bwon'?t\b|\bnot until\b|\bbefore we\b|\brequire"},
    {"key": "process", "label": "How a first booking works",
     "why": "the steps a new customer goes through, in order",
     "ask": "What happens when a new customer books for the first time — a visit, an assessment, a survey, a form — before the real work?",
     "words": r"\bfirst\b|\bnew client|\bnew customer|\bassessment|\bsurvey|\bconsultation|\bmeet|\bstep|\bprocess|\bthen we\b|\bbefore the first"},
    {"key": "cadence", "label": "How often work repeats",
     "why": "the cycle it books and reminds on",
     "ask": "Does the work repeat — every few weeks, seasonally, annually? How far ahead do you book the next one, and when do you remind people?",
     # No bare "weekly"/"fortnightly": a PRICES note ("3 hours fortnightly")
     # lit this kind and the interview never asked how the work repeats
     # (run #9, 2026-09-05). The cadence note says how the cycle RUNS.
     "words": r"\bevery \d+|\bevery (?:week|other|fortnight|month|quarter|year|season)|\bquarterly|\bannual|\bseasonal|\bcycle|\bremind|\bongoing|\bstanding (?:slot|booking|order)|\brecurring|\bregular slot|\bsame (?:day|weekday|time|slot)|\bnext visit|\buntil (?:you|they) (?:tell|say|stop|cancel)|\bre-?book"},
    {"key": "payment", "label": "Payment terms",
     "why": "when and how people pay, deposits, accounts",
     "ask": "How and when do customers pay — on the day, on invoice, a deposit up front, accounts for regulars, card or transfer?",
     "words": r"\bpay\b|\bpayment|\bdeposit|\binvoice|\bterms\b|\bcard\b|\btransfer|\baccount\b|\bdays\b.*\binvoice|\bcash\b"},
    {"key": "safety", "label": "Safety and refusals",
     "why": "what it tells customers up front and when it walks away",
     "ask": "Anything about safety or conditions on site — who needs to be there, what you'll walk away from, what the customer must have ready?",
     "words": r"\bsafe|\bsafety|\bheld\b|\bhold\b|\bunattended|\bleave the\b|\bwalk away|\bmuzzle|\bsecure|\bhazard|\bPPE\b|\brisk"},
    {"key": "coverage", "label": "Where you work",
     "why": "so it declines the wrong postcode politely",
     "ask": "Where do you cover — towns, a radius, postcodes — and anywhere you don't go or charge extra for?",
     "words": r"\bcover\b|\bcovers\b|\bcoverage|\barea\b|\bmiles\b|\bradius|\bpostcode|\bwithin \d+|\btravel\b|\bwe don'?t cover|\bnot (?:in|to) \b"},
    {"key": "hours", "label": "Hours and availability",
     "why": "when it may offer times, and when it says you're closed",
     "ask": "When are you open or available — days, hours, weekends, holidays — and how much notice do you need?",
     # No bare "hours"/"notice": "3 hours a visit" and "48 hours' notice" are a
     # price and a rule, not opening hours (run #9).
     "words": r"\bopen\b|\bopening\b|\bmon(?:day)?\b|\btue|\bwed|\bthu|\bfri|\bsat(?:urday)?\b|\bsun(?:day)?\b|\b\d{1,2}(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\bweekend|\bclosed\b|\bbank holiday|\bavailab"},
]

_BY_KEY = {k["key"]: k for k in KINDS}

# What a note's LABEL is allowed to say to claim a kind outright — the owner
# (or the assistant) named it, so one match is enough.
LABEL_WORDS = {
    "prices":   r"\bprice|\bpricing|\brates?\b|\bcost|\bcharges?\b|\bfees?\b",
    "rules":    r"\brules?\b|\bpolic|\bcancellation|\bjudgement|\bconditions?\b|\bnotice\b",
    "process":  r"\bfirst\b|\bnew (?:customer|client)|\bprocess|\bhow (?:it|we) work|\bgetting started|\bonboard|\bbooking works",
    "cadence":  r"\bcadence|\bhow often|\bfrequen|\bschedul|\brecurr|\brepeat|\bregular|\bvisits?\b|\bcycle",
    "payment":  r"\bpay|\binvoic|\bdeposit|\bterms\b|\bbilling",
    "safety":   r"\bsafe|\brefus|\binsur|\bstaff|\bvetting|\bdbs\b|\bwon'?t do|\bdon'?t do|\btrust|\bguarantee|\bpromise",
    "coverage": r"\bcover|\barea|\bwhere we|\bpostcode|\bradius|\btravel",
    "hours":    r"\bhours?\b|\bopening|\bavailab|\bwhen we|\bopen\b",
}


def _alternatives(pattern: str) -> list[str]:
    """Split an or-pattern on its TOP-LEVEL bars only — a `|` inside a group
    such as (?:week|other) belongs to that alternative."""
    out, depth, cur = [], 0, []
    for ch in pattern:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [a for a in out if a]


def _distinct_hits(pattern: str, hay: str) -> int:
    """How many DIFFERENT alternatives of an or-pattern match — two mentions of
    the same word are one hit."""
    return sum(1 for alt in _alternatives(pattern) if re.search(alt, hay, re.IGNORECASE))


def classify(label: str, text: str = "") -> list[str]:
    """Which kinds one note covers, from its label and opening text.

    A kind is claimed by its LABEL (one match of the kind's label words) or by
    its TEXT on at least two distinct keyword hits. One stray word used to be
    enough: the Prices note alone ("3 hours fortnightly") lit hours AND
    cadence, the strip read 3 of 8, and the interview stopped without ever
    asking about either (run #9, 2026-09-05)."""
    label = label or ""
    body = (text or "")[:800]
    out = []
    for k in KINDS:
        if re.search(LABEL_WORDS[k["key"]], label, re.IGNORECASE) \
                or re.search(k["words"], label, re.IGNORECASE) \
                or _distinct_hits(k["words"], body) >= 2:
            out.append(k["key"])
    return out


def coverage(sources: list[dict]) -> dict:
    """{covered: [kind…], missing: [kind…], by_kind: {key: [source labels]}}
    over the tenant's sources ([{source, text?}]). Empty sources → nothing
    covered, everything missing."""
    by_kind: dict[str, list[str]] = {k["key"]: [] for k in KINDS}
    for s in sources or []:
        label = str(s.get("source") or s.get("label") or "")
        for key in classify(label, s.get("text") or s.get("content") or ""):
            by_kind[key].append(label)
    covered = [k for k in KINDS if by_kind[k["key"]]]
    missing = [k for k in KINDS if not by_kind[k["key"]]]
    return {"covered": covered, "missing": missing, "by_kind": by_kind,
            "score": f"{len(covered)} of {len(KINDS)}"}


def ask_text(missing: list[dict], business_words: str = "", website: bool = False) -> str:
    """The silent auto-ask for the knowledge pages: ask for the kinds not
    covered, three at a time, in the business's own terms. With `website`
    (the page offers "Start from your website"), the interview opens by
    offering that route first — a site is the fastest way to depth, and the
    assistant never mentioned it (run #9, 2026-09-05)."""
    if not missing:
        return ("Every kind of fact the agent needs is covered. Ask whether any note "
                "needs a number or a rule made sharper, else say the knowledge base is "
                "in good shape.")
    names = ", ".join(k["label"].lower() for k in missing)
    first = missing[:3]
    qs = " ".join(k["ask"] for k in first)
    lead = ""
    if website and len(missing) == len(KINDS):
        lead = ("First, in one line, ask whether I have a website: if I do, tell me to "
                "paste it into 'Start from your website' on this page (it reads my key "
                "pages and drafts the notes for me to review) and to come back to you "
                "for whatever it missed. Then, in the same message, ")
    body = (f"Interview me for the facts my agent still can't answer: {names}. "
            f"Ask about these first, one short question each — at most three questions "
            f"in this turn, numbered — in my trade's words"
            + (f" ({business_words})" if business_words else "") + f": {qs} "
            "Then write the notes from what I said, and nothing I didn't.")
    return lead + (body[0].lower() + body[1:] if lead else body)


def prompt_block(cov: dict) -> str:
    """The line for the assistant prompts: what's covered, what isn't, and how
    to write a note. Empty coverage still says what to collect."""
    lines = ["Knowledge coverage (the kinds of fact the agent needs; write notes ONLY from what "
             "the customer says — never invent a price, a rule or an hour):"]
    for k in KINDS:
        have = cov["by_kind"].get(k["key"]) or []
        lines.append(f"- {k['label']} — {k['why']}: "
                     + ("covered (" + ", ".join(have[:3]) + ")" if have else "NOT YET — ask: " + k["ask"]))
    saved = sorted({lbl for labels in cov["by_kind"].values() for lbl in labels},
                   key=str.lower)
    if saved:
        lines.append("Notes ALREADY SAVED (never propose these again — a saved note only "
                     "reappears when the customer asks to change it, and then as ONE note "
                     "under the same label): " + "; ".join(saved[:40]))
    if cov.get("review_topics"):
        lines.append("Review status: keyword coverage above is only saved-note presence, not proof that every detail is answered. Pending drafts are NOT saved facts.")
        for topic in cov["review_topics"]:
            pending = ", ".join(n["label"] for n in topic["pending"][:5])
            unknown = "; ".join(q["question"] for q in topic["questions"] if q["status"]=="unknown")
            lines.append(f"- {topic['label']}: {topic['state']}; pending drafts: {pending or 'none'}; unanswered: {unknown or 'none recorded'}. Ask only for actual gaps; never infer an answer from silence.")
    lines.append("When you have facts, put each as its own note in `notes` (or the `knowledge` "
                 "recommend page as \"Label | content\"): a short label in their words and the "
                 "fact stated plainly IN THE BUSINESS'S OWN VOICE. Keep the owner's first-person wording "
                 "and any explicit writing preference. Keep numbers exact, one clear subject per note. Propose "
                 "ONLY the new notes from this turn's answers — never a fact lifted from another "
                 "note or the website, never an inference. Ask about at most three kinds per "
                 "turn — count them. 'Covered' means SOME note of that kind exists: when the "
                 "covering note is plainly partial (one item priced, not the main services; the "
                 "hours of one day), still ask for the rest — Prices is only really covered when "
                 "the main services carry a price.")
    lines.append(SUBJECT_NOTES)
    return "\n".join(lines)


def parse_notes(values) -> list[dict]:
    """Recommend-card values [{name:'note', value:'Label | content'}] → notes."""
    out = []
    for v in values or []:
        if not isinstance(v, dict) or v.get("name") != "note":
            continue
        raw = str(v.get("value") or "")
        label, _, content = raw.partition("|")
        label, content = label.strip()[:80], content.strip()[:4000]
        if label and content:
            out.append({"label": label, "content": content})
    return out[:20]


def _gist(text: str) -> str:
    """A note's words, lowercased and stripped of punctuation — what two notes
    have to differ in to be two notes."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _same_note(a: str, b: str) -> bool:
    """Two notes saying the same thing in different words (run #10 (ooo): the
    label dedupe passed a reworded duplicate straight through). Word overlap
    against the shorter note, plus a sequence ratio — either at 0.82 counts."""
    ga, gb = _gist(a), _gist(b)
    if not ga or not gb:
        return False
    if ga == gb:
        return True
    wa, wb = set(ga.split()), set(gb.split())
    overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
    return overlap >= 0.82 or SequenceMatcher(None, ga, gb).ratio() >= 0.82


def clean_notes(notes, *, deduplicate=True) -> list[dict]:
    """The onboarding reply's `notes` list, tidied: label + content, no blanks,
    no duplicates by label — and none that merely rewords one already in the
    list (run #10 (ooo))."""
    seen, out = set(), []
    for n in notes or []:
        if not isinstance(n, dict):
            continue
        label = " ".join(str(n.get("label") or "").split())[:80]
        content = str(n.get("content") or "").strip()[:4000]
        if not label or not content or (deduplicate and label.lower() in seen):
            continue
        if deduplicate and any(_same_note(content, o["content"]) for o in out):
            continue
        seen.add(label.lower())
        out.append({"label": label, "content": content})
    return out[:20]
