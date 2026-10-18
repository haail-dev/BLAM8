# Guided Knowledge review

When source review is enabled, the onboarding Concierge starts from the current
pending notes rather than sending every message to the source correction tool.
It shows a note's actual title and text, then offers keep, leave out or correction.
Supporting source wording is handled with its note: the owner may explicitly
retain the original wording together, or inspect the differences. This choice
can preserve headings and repetition verbatim; it is not semantic deduplication.
The guide never silently excludes source text or declares a paraphrase complete.

The `/onboarding/knowledge/review/{review_id}/guide` endpoint reads the same
tenant-scoped, versioned review used by admission. It returns a bounded patch to
the existing review form, not a database mutation. Model interpretation is used
for natural messages; Show and explicit action buttons do not call a model.
The action is restricted to the displayed item, with exact existing note targets.
Owner answers and exclusion reasons must occur in the owner's message. Unknown
details can remain unknown. Reopening a named note changes its review decision,
not its text. Wording/source repairs still use the existing source-correction
proposal flow and require review/application there.

The guide handles original source wording, overlap acknowledgements and optional
questions in plain language. It preserves shared qualifications and requires
the existing admission planner to succeed before offering Save. Nothing is
saved by chat decisions: the separate Save reviewed decisions action submits
the ordinary form, whose permission/version/source/overlap checks still apply.
It does not navigate into later onboarding steps or override sending settings.

The browser checks that the form and its local values have not changed while a
turn is in flight. Old action buttons are invalid after another choice, manual
edits or form replacement. Guide requests participate in the existing busy
guard, preventing website import/correction/save races. Unsaved review choices
remain browser form state; a full reload can lose them. They are not advertised
as persisted preferences or saved Knowledge.

Detailed source inventory presentation belongs to the separate Knowledge UI
change. Removing its visible block must preserve hidden `source_<id>`,
`source_reason_<id>` and `source_default` controls for admission. Preview renders
should also carry `guided_unknown_<id>` and `guided_expand_<id>` hidden fields
from the submitted review form to retain guide progress. These markers are only
review navigation state, never evidence of a business fact.
