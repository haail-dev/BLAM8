"""Customer promises rendered from stored offer expiries, never inferred by a model."""
from datetime import datetime
import re

from app import dates


def expiry(row: dict) -> datetime:
    return datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))


def display(row: dict, now: datetime | None = None) -> dict:
    from app.scheduling import DIARY_TZ
    exp = expiry(row)
    local = exp.astimezone(DIARY_TZ)
    return {"until_iso": exp.isoformat(),
            "until": f"{dates.pretty_date(local)}, {local:%H:%M}",
            "until_phrase": dates.deadline_phrase(exp, now)}


def slot_label(row: dict) -> str:
    from app.scheduling import window_words
    from app import slotgrid
    day = str(row.get("booking_date") or row.get("date"))[:10]
    label = dates.pretty_day(datetime.fromisoformat(day).date())
    if row.get("_arrival") and row.get("_departure"):
        arrival = datetime.fromisoformat(row["_arrival"])
        departure = datetime.fromisoformat(row["_departure"])
    else:
        span = slotgrid.stay_span(row)
        arrival = slotgrid.abs_to_datetime(span[0]) if span else None
        departure = slotgrid.abs_to_datetime(span[0] + span[1]) if span else None
    if arrival and departure and arrival.date() != departure.date():
        return (f"{label} at {arrival:%H:%M} to "
                f"{dates.pretty_day(departure.date())} at {departure:%H:%M}")
    return f"{label} {window_words(row.get('window'))}"


def deadline_line(rows: list[dict], now: datetime | None = None) -> str:
    if not rows:
        return ""
    if len({expiry(r) for r in rows}) == 1:
        subject = "this" if len(rows) == 1 else "these"
        return f"I'm holding {subject} for you until {display(rows[0], now)['until_phrase']}."
    from app.scheduling import window_words

    days = [datetime.fromisoformat(str(r.get("booking_date") or r.get("date"))[:10]).date()
            for r in rows]
    labels = []
    for r, day in zip(rows, days):
        full = slot_label(r)
        if " to " in full:
            # A stay must still identify its arrival and departure.
            labels.append(full)
            continue
        same_weekday = [d for d in days if d.weekday() == day.weekday()]
        label = dates.pretty_day(day) if len(set(same_weekday)) > 1 else day.strftime("%A")
        if days.count(day) > 1:
            window = {"am": "morning", "pm": "afternoon", "eve": "evening",
                      "day": "during the day"}.get(r.get("window"), window_words(r.get("window")))
            label += f" {window}" if window else ""
        labels.append(label + "'s")

    # Dict insertion order follows the first offered slot in each expiry group.
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(expiry(r), []).append(i)
    clauses = []
    for group_number, indices in enumerate(groups.values()):
        phrase = display(rows[indices[0]], now)["until_phrase"]
        if group_number == len(groups) - 1:
            count = {1: "one", 2: "two"}.get(len(indices), str(len(indices)))
            clauses.append(f"the other {count} until {phrase}")
        else:
            names = [labels[i] for i in indices]
            if any(" to " in name for name in names):
                names = [name if " to " in name else name + " slot" for name in names]
            subject = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            if all(" to " not in name for name in names):
                subject += " slot" if len(names) == 1 else " slots"
            verb = "is" if len(names) == 1 else "are"
            clauses.append(f"{subject} {verb} held until {phrase}")
    return ", ".join(clauses) + "."


def enforce(reply: str, lines: list[str]) -> str:
    """Replace deadline sentences without rewriting the surrounding email."""
    lines = list(dict.fromkeys(line for line in lines if line))
    if not lines or all(line in reply for line in lines):
        return reply
    missing = [line for line in lines if line not in reply]
    temporal = (r"\b(?:today|tonight|tomorrow|midnight|noon|Monday|Tuesday|Wednesday|"
                r"Thursday|Friday|Saturday|Sunday|this (?:evening|afternoon|morning)|"
                r"next (?:week|day)|\d{1,2}:\d{2}(?:\s*[ap]m)?|\d{1,2}\s*[ap]m|"
                r"(?:\d+|one|two|three|four|twenty-four) (?:hours?|days?))\b")
    deadline_claim = re.compile(
        r"\b(?:hold|holding|held|reserve[ds]?|reservation|keep|kept)\b.*?"
        r"\b(?:until|till|through)\b.*?" + temporal + r"|"
        r"\b(?:hold|holding|held|reserve[ds]?|keep|kept)\b.*?\bfor "
        r"(?:\d+|one|two|three|four|twenty-four) (?:hours?|days?)\b|"
        r"\b(?:expire[sd]?|expiry|release[ds]?|deadline)\b.*?" + temporal + r"|"
        + temporal + r".*?\b(?:expire[sd]?|release[ds]?)\b|"
        r"\byou have until\b.*?" + temporal, re.I | re.S)
    # Keep the separators as literal text: never reflow paragraphs or signatures.
    parts = re.split(r"((?:\r?\n[ \t]*){2,})", reply)
    removed_leading = set()
    for i in range(0, len(parts), 2):
        paragraph = parts[i]
        protected = [(m.start(), m.end()) for line in lines
                     for m in re.finditer(re.escape(line), paragraph)]
        removals = []
        for sentence in re.finditer(r"\S.*?(?:[.!?](?=\s|$)|$)", paragraph, re.S):
            if any(sentence.start() < end and sentence.end() > start
                   for start, end in protected):
                continue
            if deadline_claim.search(sentence.group()):
                end = sentence.end()
                while end < len(paragraph) and paragraph[end] in " \t":
                    end += 1
                removals.append((sentence.start(), end))
        if removals and removals[0][0] == len(paragraph) - len(paragraph.lstrip(" \t")):
            removed_leading.add(i)
        for start, end in reversed(removals):
            paragraph = paragraph[:start] + paragraph[end:]
        parts[i] = paragraph

    clean = "".join(parts)
    newline = "\r\n" if "\r\n" in reply else "\n"
    insertion = newline.join(missing)
    closing = re.search(
        r"(?im)^[ \t]*(?:kind regards|best regards|warm regards|regards|best wishes|"
        r"many thanks|thanks|thank you|cheers|yours sincerely|yours faithfully|best)"
        r"[,!.]?[ \t]*\r?$", clean)
    closing_at = closing.start() if closing else len(clean)
    if not closing:
        # A bare name on the final line is also a signature, even without a sign-off.
        candidates = list(re.finditer(
            r"(?m)^[ \t]*[A-Z][a-z]+(?: [A-Z][a-z]+){0,3}[ \t]*\r?$", clean))
        if candidates and not clean[candidates[-1].end():].strip():
            closing_at = candidates[-1].start()
    offset, offered = 0, None
    for i, paragraph in enumerate(parts):
        if i % 2 == 0 and offset + len(paragraph) <= closing_at and not deadline_claim.search(paragraph):
            times = list(re.finditer(temporal, paragraph, re.I))
            if times and (re.search(r"\b(?:slots?|options?|available|could do)\b", paragraph, re.I)
                          or len(times) >= 2):
                offered = i
        offset += len(paragraph)
    if offered is not None:
        # A removed standalone deadline immediately after the times leaves its
        # original paragraph available for the replacement.
        if offered + 2 in removed_leading:
            remainder = parts[offered + 2]
            parts[offered + 2] = insertion + (" " if remainder else "") + remainder
        else:
            parts[offered] += newline * 2 + insertion
        return "".join(parts)
    if closing_at < len(clean):
        return clean[:closing_at] + insertion + newline * 2 + clean[closing_at:]
    return clean + (newline * 2 if clean else "") + insertion
