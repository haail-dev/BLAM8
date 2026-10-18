"""Shared Jinja2 templates instance (used by main and the dashboard router)."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.dates import pretty_date, sent_when

class _TimedTemplates(Jinja2Templates):
    """Jinja2Templates whose rendering shows up as a 'render' step in the opt-in startup timing
    (a no-op without the header): how much of a page's cost is template work."""

    def TemplateResponse(self, *args, **kwargs):
        from app.startup_timing import span
        with span("render"):
            return super().TemplateResponse(*args, **kwargs)


templates = _TimedTemplates(directory=str(Path(__file__).parent / "templates"))

# `{{ some_date | pretty_date }}` → "11th Sep 26" (src/app/dates.py). Registered
# here so every template shares the wording the agents write into their own
# summaries.
templates.env.filters["pretty_date"] = pretty_date
# `{{ e.sent_at | sent_when }}` → "14:32" today, "22nd Sep 26 · 14:32" after.
templates.env.filters["sent_when"] = sent_when


def window_label(window) -> str:
    """Operator-facing words for a booking window code: am/pm/day read as
    Morning / Afternoon / Whole day; a clock time passes through (live
    2026-09-04: the job page read "10th Sep 26 . pm")."""
    w = str(window or "").strip()
    return {"am": "Morning", "pm": "Afternoon", "day": "Whole day",
            "eve": "Evening (out of hours)"}.get(w, w)


templates.env.filters["window_label"] = window_label


_GAP_LEAD = ("whether ", "what ", "if ", "how ", "when ", "where ", "which ", "who ", "why ",
             "the ", "a ", "an ")


def gap_title(gap) -> str:
    """A knowledge-source title from a knowledge-gap QUESTION: the lead-in word
    goes, it ends on a whole word within 48 characters, first letter up.
    (Onboarding film, 30 Sep 2026: a gap question saved as its own 80-character
    cut, ending mid-sentence on "offers and", was on the Knowledge step as a
    source name.)"""
    text = " ".join(str(gap or "").split())
    low = text.lower()
    for lead in _GAP_LEAD:
        if low.startswith(lead):
            text = text[len(lead):]
            break
    if len(text) > 48:
        cut = text[:49]                      # one over, so a word ending exactly at 48 survives
        text = cut[:cut.rfind(" ")] if " " in cut else cut[:48]
    text = text.rstrip(" ,;:-").rstrip()
    for tail in (" and", " or", " is", " are", " of", " the", " a", " an", " to", " for"):
        if text.lower().endswith(tail):
            text = text[:-len(tail)].rstrip()
    return (text[:1].upper() + text[1:]) if text else ""


templates.env.filters["gap_title"] = gap_title
