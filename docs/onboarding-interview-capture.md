# About you: capture and review

The Step 1 Assistant maps business facts and customer scope into the business
brief, and already-supplied work requests into Goals in the same turn. Summaries
must retain material qualifiers such as fictional/test-only scope, exclusions,
human review and work that remains with the owner's team. Describing a goal does
not apply a sending control or enable an operating system or integration.

Once the step has sufficient information, extra scheduling, CRM, subject and
optional website questions are not prerequisites. A request to pause leaves the
form available for review without another question or an invitation to advance.
Instructions to stop, leave a banner blank or avoid creating records do not need
invented persistent settings. Supported facts for later pages can use the existing
held-suggestion mechanism when appropriate; they must not be filed as writing style.

After a successful interview turn, the widget supplies a factual receipt:
form edits await submission and sending settings were not changed by that turn.
Only server-confirmed held receipts are described as recorded suggestions; those
are distinct from applied settings and saved Knowledge. No new held receipt means
no new item recorded in that turn, not that earlier held items do not exist.

`tests/test_onboarding_capture.py` covers the actual model-call prompt contract,
separate field transport, no configuration writes before submission, and explicit
form persistence across three synthetic business descriptions. Its Node replay
executes the widget turn/fill code without a browser, provider or real tenant,
including held/no-held receipts, provider failure and the unchanged Voice path.

These checks use fake model responses. They establish offline contracts and
persistence boundaries, not live model extraction quality. PM-ONB-001 was observed
on revision `2e081d1` on 18 October 2026; this fix still needs an authorized live
retest after release. The report did not contain a false saved claim or evidence
of downstream operational actions. No migration or live data change is required;
rollback is a code revert.

The sole changed agnostic render is `platform-interview.json`. Its complete
model input was reviewed: reversing only the Step 1 guidance and pause-rule
changes reproduces the prior render hash exactly. No vocabulary debt exemptions,
other render baselines, outcome rules or guard assertions were changed.
