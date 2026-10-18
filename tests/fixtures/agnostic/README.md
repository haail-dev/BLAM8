# S1a real-assembly baselines

## Retained-source Knowledge corrections — 18 October 2026

The platform-help record changes only its content and definition version hashes
after the Knowledge evidence map and restricted guide were updated. Restoring
the previous two version values makes the complete render match the previous
record exactly. No prompt wording or permissions in that record changed. The
complete source scan reports no added, removed or re-keyed wording debt; guard
detectors are unchanged. The new source-correction caller has separate offline
tests for the actual model request, strict output validation, source preservation
and refusal to turn prose-only output into draft mutations.

## Link kinds: inbox, invoices, quotes, money — 16 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants). A link labelled "Invoices" took the owner to Plans & billing (the BLAM8 subscription page) because the agent had no invoices kind to name. The prompt's LINKS clause now names four more kinds (`inbox`, `invoices`, `quotes`, `money`, each with id `''`) and says what they are and never to use `billing` for them; the `links` tool schema's `kind` enum gains the same four. `nudge-concierge_action` carries the sentence twice and `model-warm` only the enum.

Review method (strip-the-clause, scripted): for each of the ten candidates, put the old sentence back in its place and drop the four added enum values; the complete render hashes to exactly the previous baseline (all ten, checked before any baseline was written). No other text moved.

A stubbed model proves the request, not its reaction to new wording; the rig proves that (ask "Show me my invoices" and "Open the inbox" by text and by voice after a deploy: the chip should open /app/invoices and /app/conversations, never Plans & billing).

## Ledger routing: status from the records, what was said from the chat — 16 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants) for one change: a question about the STATUS or OUTCOME of one of the asker's own items ("what happened to the text to Pat", "did that go", "where are we with Tom") is routed to `list_awaiting_approval` in the same turn, instead of being answered from the chat or the typed task note. (1) Rule 3b's clause "what did you do / what have I signed off / what happened to that one / where are we with X" is replaced by a rule that names those status questions, says why the chat cannot answer them (results land after a turn ends; the chat can be out of date or empty after a New task), keeps what was only SAID or ASKED in the recent turns answered from the chat with no call, and forbids "I don't have a task for X" / "nothing happened" about anything not in the recent turns without a ledger check. (2) Rule 3 gains one sentence: never announce a check ("Checking your inbox now.") that the turn does not make. (3) Rule 11's `list_awaiting_approval` line gains "what happened to X / did that go". (4) The `list_awaiting_approval` description gains the same examples and the "results land after a turn ends, so call this for any status or outcome, not the chat; what was only said or asked needs no call" sentence. The tool order is unchanged (still the last tool). The native voice prompt (`gptlive.PROMPT`) and the delegated task note's header (`concierge_delegation._NOTE_HEAD`, which says the list is only what was started from here recently, not the whole record) also changed, but neither is part of any render record.

Review method (strip-the-clause, scripted from this wording rather than from the source): export every real-caller render from this tree with `tests/agnostic_review.py` and compare with the baseline records committed on `origin/main`; for each of the ten records that differ, take the rule 3 sentence out, put the rule 3b block and the rule 11 line back to the text they displaced, and swap `list_awaiting_approval` for the one on `origin/main`, and `guard.render_record(...)` equals the previous committed baseline for all ten (30 strings restored, 13 tool definitions swapped). A deliberately wrong old rule 3b clause fails the nine records that carry the prompt; `model-warm` carries only the tool definition, which the swap covers. The other 200 records are byte-identical to their baselines. The records were regenerated from the exported after-renders with `render_record`, not edited by hand. The exported debt diff is empty (no debt entry changed or was re-keyed), no new debt and no detector change; the new strings carry no flagged word.

A stubbed model proves the request, not its reaction to new wording; the rig proves that (re-run "What happened to the text to Pat?" after a deploy, typed, after pressing New task: it should call `list_awaiting_approval` and answer from the item's state).

## Intake receipt and the ledger's discovery wording — 16 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants) for one change: when one message asks for several things, what became of each part is recorded (R6). (1) `start_tasks` gains an optional `others` property, an array of `{said, how, because}` with `how` one of answered, cannot or asking, for every part that is not a task; its description (and the delegated form's) says each part is "answered/declined in `now` and listed in `others`". (2) Rule 10 gains one sentence in both of its forms ("Also list each part that is not a task in `others` (the owner's own words and how it ended) so it is on record."). (3) The `list_awaiting_approval` description says that 'recent' and 'all' also show what became of each part of a message that asked for several things at once. The tool order is unchanged (it is still the last tool). The discovery capability list's wording for the same tool changed too, but discovery is not part of any render record.

Review method (strip-the-clause, scripted from this wording rather than from the source): export every real-caller render from this tree with `tests/agnostic_review.py` and compare with the baseline records committed on `origin/main`; for each of the ten records that differ, take the rule 10 sentence back out and swap the three changed tool definitions (`start_tasks` in its plain and delegated forms, `list_awaiting_approval`) for the ones on `origin/main`, and `guard.render_record(...)` equals the previous committed baseline for all ten (3 sentences removed, 19 tool definitions swapped). A deliberately wrong old sentence fails the three records that carry rule 10 (`route-concierge-typed`, `-voice`, `-staff`); the other seven differ only by tool definitions, which the swap covers. The other 200 records are byte-identical to their baselines. The records were regenerated from the exported after-renders with `render_record`, not edited by hand. The exported debt diff is empty (no debt entry changed or was re-keyed), no new debt and no detector change; the new strings carry no flagged word.

## Work ledger inside `list_awaiting_approval`, and its routing lines — 16 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants) for one change: the Concierge can read its own work. (1) The existing read-only tool `list_awaiting_approval` (still the LAST tool of the shared catalogue, so no tool index moved) is extended, not joined by a second tool: its description now covers the asker's own open and recent work with a plain state, and its schema gains optional `status` (open, recent, all), `since_days`, `customer` and `limit`. (2) Rule 3b gains a clause: "what did you do / what have I signed off / what happened to that one / where are we with X" use the same call with status 'recent' or 'all', the Concierge says only what each item's state says, and says "signed off" only when the item does; a bare "outstanding" also calls it in the same round. (3) Rule 11 (WHICH TOOL FIRST) extends its existing `list_awaiting_approval` line to the same questions. The native voice prompt (`gptlive.PROMPT`) also gained its delegate-list phrase, but it is not part of any render record.

Review method (strip-the-clause, scripted from this wording rather than from the source): export every real-caller render from this tree with `tests/agnostic_review.py` and compare with the baseline records committed on `origin/main` (`d95a11e4`); for each of the ten records that differ, put the rule 3b clause and the rule 11 sentence back to the text they displaced and swap the extended `list_awaiting_approval` definition back to the parent tree's, and `guard.render_record(...)` equals the previous committed baseline for all ten (20 strings restored, 13 tool definitions swapped). A deliberately wrong old rule 11 sentence fails nine of the ten (`model-warm` has no prompt text), so the check discriminates. The other 200 records are byte-identical to their baselines. The records were regenerated from the exported after-renders with `render_record`, not edited by hand. The exported debt diff is empty (no debt entry changed or was re-keyed), no new debt and no detector change; the new strings carry no flagged word.

## Ripples switch stamp in the shared theme head: `template-portal` — 16 October 2026

One render record changed: `template-portal` (all three clock variants share it). `src/app/templates/_theme_head.html` is included by the portal, and it now also stamps `data-water` before first paint and defines `window.blam8ApplyWater` (the Ripples switch, a per-device setting beside Light / Dark). The portal does not opt in to the water module (`water_page` is set only by the app and onboarding shells), so the only change to its render is that added script block.

Review method (strip-the-clause): take the complete candidate render, remove exactly the added block (from the `// Ripples:` comment through the `window.blam8ApplyWater` line) and the result equals the previous baseline hash exactly (`4a2983e5...`). No vocabulary was added: the new lines carry no flagged word, and no detector or debt entry changed.

## Draft edit and delete review — 15 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants) for one change: the Concierge may now edit or delete a DRAFT invoice or quote, always on a confirm card (scenario N20). (1) The action tool gains four kinds (`invoice_edit_draft`, `invoice_delete_draft`, `quote_edit_draft`, `quote_delete_draft`), in the card's kind enum and in a bulk item's, and a string field `lines` (the new full set of lines); the voice schema's allowed-keys list names it too. (2) The prompt's invoice and quote kind lists gain a block each ("Two more are for a DRAFT invoice only: …"): write the complete new set of lines, only for a draft, never for a sent one, ask which draft (naming each by number, date and amount) and whether to change it or delete it. (3) Rule 1's sentence about deleting no longer sends deletes "to the main app" (it has no invoice delete) and excepts a DRAFT invoice or quote. (4) The "what you can do" section says it can edit or delete a draft once the owner taps Confirm, that a deleted draft cannot be brought back, and that it cannot change or delete a sent one, a contact, a booking or a setting; the questions-only surfaces say they cannot delete or change records from here. (5) The `list_invoices` and `list_quotes` tool descriptions say a draft's lines are included.

Review method (strip-the-clause, scripted from this wording rather than from the source): export every real-caller render from the parent tree and from this one; for each of the ten records, take the new export and put each clause above back to the text it displaced (the old rule-1 sentence; the two "Two more are for a DRAFT … only" blocks removed; the capability sentence in its acting and questions-only forms; the two tool descriptions; the four kinds out of both enums and `lines` out of the schema and out of the voice allowed-keys list), and `guard.render_record(...)` equals the previous committed baseline for all ten. A deliberately wrong old sentence in that script fails seven of the ten (the three without rule 1 still pass), so the check discriminates. The other 200 records are byte-identical between the two exports. The records were regenerated from the exported after-renders with `render_record`, not edited by hand. No debt entry changed or was re-keyed (the exported debt diff is empty), no new debt, no detector change; the new strings carry no business or trade names.

A stubbed model proves the request, not its reaction to new wording; the rig proves that (N20, both turns: it should ask which of the two drafts, stage a confirm card, and say nothing is done until the tap).

## Deterministic sign-off review — 15 October 2026

109 render records changed, for one change in two parts. (1) The reply prompt (`agent.build_system_prompt`) and the outbound prompt (`agent.build_outbound_prompt`) no longer say "End every reply with this sign-off, exactly:" / "End the message with this sign-off, exactly:". For an email whose business has a sign-off they now say the system adds that exact text after the reply (or message), ask the model not to write it or any other closing line, name or signature, and still carry the sign-off text as data (so the cross-tenant pin in `test_e5_mailbox_voice_does_not_cross_into_another_business` stays meaningful and unchanged). No sentence is emitted for SMS or when no sign-off is set. (2) `agent.draft_reply` and `agent.compose_outbound` append the configured sign-off in code after composition (`src/app/sign_off.py`), so the drafts the job records store (the harness tenant's sign-off is "Thanks") now end with a blank line and the sign-off. Before this, the model was asked to write the sign-off and about one reply in six did not.

The records, by family: the 80 `<shape>-draft` and `<shape>-redraft` records for the 40 shapes that reach the model (`read_error-*` never calls it and is unchanged); `nudge-reservation`, `nudge-timing`, `nudge-waiting_list`; the 25 `job-*` records (`office`, `stay`, `tms`, `fsm`, `nudge`, `invoice-0|7|14|30`, `service`, `recall`, `appointment-24h|2h|7d`, `no-show`, `ready`, `stale-stock`, `best-before`, `deposit`, `handover-paid`, `handover-unpaid`, `owner-service`, `quote`, `events`, `sequence`); and `provenance-kb`.

Review method (strip-the-clause, two moves, scripted from this wording rather than from the source): for each candidate, (1) put the old sentence back in its place (the reply form in `-draft`, `-redraft`, `nudge-*` and `provenance-kb`, the message form in `job-*`) and (2) remove the appended blank line and sign-off from every stored draft the record captures (`insert_email_log` positional argument 4, `update_email_log_draft` argument 1, inside `queued`, `writes` and `source_receipts`); then `guard.render_record(serial(...))` equals the previous baseline for all 109, so nothing else moved. The `-draft`, `-redraft` and `nudge-*` records capture only the model request, so only move 1 applies to them. In `job-quote` the job appends the quote link after the composed text, so the appended sign-off sits just before the link block rather than at the end of the stored draft; that is where move 2 removes it. The records were regenerated from the exported after-renders with `render_record`, not edited by hand.

The other 101 records are identical: every `-dispatch` record, `outcome-*`, `portal-*`, `template-portal`, `platform-*`, `extractor-*`, `model-*` (including `model-voice-sample`, `model-voice-learning` and `model-warm`), `concierge-*`, `route-concierge-*`, `dispatch-*`, `nudge-portal_action`, `nudge-concierge_action` and `read_error-*`. No debt entry changed or was re-keyed, no new debt, no detector change. One side effect worth naming: the "no reply needed" note the model writes for an inbound it classed as not needing a reply is stored as the draft text and now also ends with the sign-off. The row is stored skipped and is never sent without a Redraft, so the effect is cosmetic.

The outbound sentence was reworded after review: the 25 `job-*` records no longer say "even if the examples below end with one" (that prompt has no examples), so they were refreshed a second time from the exported renders; the strip-the-clause proof was re-run on all 109 with the new message-form wording, and the other 84 changed records and the 101 unchanged records did not move.

A stubbed model proves the request, not its reaction to new wording; the rig proves that (a drafted reply and a Concierge email card on the rig must each end with the sign-off exactly once).

## GPT-Live wave 3a review fixes — 15 October 2026

Nine render records changed again (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget`; `model-warm` is unchanged, its tool definitions did not move) for three wording fixes from the review of the entry below. (1) In the voice style, the proposal bullet no longer forbids "Shall I send it?": it said never to ask it "about a card", which contradicted the unchanged writing-emails bullet and the Standard-voice rule that a bare yes sends a waiting draft only after that question was asked; the bullet now keeps the draft question and says the GPT-Live note overrides it (voice records only: `concierge-voice`, `nudge-concierge_action`, `route-concierge-voice`). (2) The canned caught-up line reads "nothing needs a reply or a draft approved right now", because "approval" now means the cards in rule 3b. (3) The capability section says the Concierge cannot send anything without the owner's approval (a card, or the inbox's own approve-to-send setting) and cannot change the app's settings (prices, hours, plan); it had said "without a card" and "change prices, hours or settings".

Review method (strip-the-clause): for each of the nine candidates, put each of those three clauses back to its previous text and the complete render equals the previous baseline exactly (a script typed from the review instructions, not from the source). A fourth change, gating the voice "heard as a question, meant as a note" bullet on the Pro plan because `update_contact` is a Pro kind, changes no record: every render is a Pro prompt, so the Pro text is byte-identical. The other 201 records are identical. No new debt, no re-keyed or removed entry, no detector change.

## GPT-Live wave 3a wording review — 15 October 2026

Ten render records changed (`concierge-typed|voice|questions|task`, `nudge-concierge_action`, `route-concierge-typed|voice|staff|widget` and `model-warm`; each name is one record across its clock variants) for the ten wording changes the 14 October GPT-Live stress run (N1-N16) asked for. (1) A short "what you can do" section: look things up, changes only on Confirm, no undo, nothing for accounts, VAT, payments or texts; the questions-only surfaces say changes are made from the Concierge page. (2) Rule 5: an unnamed reference ("the quote") binds only to the previous exchange, never to restored history. (3) Rule 3b split into outstanding versus awaiting approval, rule 11 routes "awaiting my approval" to a new read-only tool `list_awaiting_approval`, and the inbox overview's description points to it. (4) The earlier-turns note explains "[Proposed: …]", "Done —" and "Cancelled — nothing changed:". (5) `update_contact` guidance: free text is a note, a field label only when the record was opened in this conversation; `create_followup` names its fields. (6) A voice bullet for a fact about one customer that speech-to-text turned into a question. (7) `get_availability` and rule 11: an `availability_unknown` result is never reported as full. (8) A new link kind `invoice_settings` (the Getting paid page) in both link enums, with rules 4 and 7 telling it apart from `billing` (the BLAM8 plan). (9) VOICE STYLE: every card is approve-first (Standard voice: yes or tap; GPT-Live: only a tap), amounts as digits, no reference codes read aloud. (10) Rule 10 and the `start_tasks` description and schema: every part of a multi-part message is a task or is answered or declined in a new optional `now` sentence.

Review method (strip-the-clause): for each candidate, put every new clause back to the text it displaced, delete the new tool and the `now` property and take `invoice_settings` out of the enums, and the complete render equals the previous baseline exactly, for all ten records (checked by a script typed from the plan, not from the source). Differences from the plan's list: `model-warm` (the start-up tool warm-up) also changed, because it sends the tool definitions; rule 3b keeps two triggers of the old rule ("what needs replying to", "what's in the inbox") that the plan's draft dropped; and the delegation-mode wording of rule 10 and of the `start_tasks` description renders only with delegation enabled, so a unit test covers it, not a baseline. The other 200 records are identical. No new debt, no re-keyed or removed entry and no detector change: `list_awaiting_approval` is the last tool of the shared catalogue (after `list_expiring`), so no tool index moved. A stubbed model proves the request, not its reaction to new wording; the rig re-test does.

## GPT-Live screen wording review — 14 October 2026

`test_shared_source_debt_is_exact_and_owned` had been failing on main (and so in CI) since the GPT-Live screen landed: `src/app/static/voice-gptlive.js` carried 15 flagged lines with no debt entries, and `src/app/templates/admin/marketing_queue.html` one. None was trade wording; all were the detector matching ordinary words. Removed rather than registered: the comment word "child", five CSS `:nth-child(n)` selectors (now `:nth-of-type(n)`, same five spans), the CSS class `vl-top-feed` / `has-live-feed` (now `vl-top-strip` / `has-live-strip`, with the two browser tests that name it) and the sample tracking reference `kennels-post-1` (now `example-post-1`). Two lines remain as exact debt, owner U21 step S4b: the iOS CSS property `-webkit-touch-callout`, which cannot be renamed. No detector change, no render baseline change.

## Structured setup rows — 4 October 2026

Seven platform records (data, help, industry, interview, knowledge, mailbox,
voice) gained only the service resource/day and booking document syntax clauses.
Removing those exact clauses from each complete captured render reproduced its
previous hash and excerpt before refreshing the record. No detector or debt
exemption changed. See [the implementation and review report](../../../reports/astra/setup-contract-2026-10-04.md).

These files originally captured existing behaviour at `936280b196a5d45dccf01488d5ad9d0bfff796fd`. They are test data, not a replacement prompt compiler. Subsequent reviewed updates are recorded below.

## S1b runtime review — 27 September 2026

Reviewed from `4a571d4`: 143 of 210 render records changed for neutral runtime wording and the generic Replied label. Full exports had no non-text value or structure changes. Debt moved from 4,064 to 2,258 occurrences: 1,827 exact stale keys removed, 21 reviewed replacements retaining their previous owners and steps. Unaffected entries were retained. Comments, docstrings and later-step mechanisms remain tracked.

See [the S1b review](../../../reports/astra/s1b-runtime-wording-2026-09-27.md) for the per-step counts, affected outcomes, review method and required rig proof. This update does not claim that a stubbed model proves its reaction to new wording.

## S1b non-runtime review — 27 September 2026

Reviewed from turn 1 commit `7a1af4a`: neutralised shared comments/docstrings and remaining generic UI examples, keeping incident dates, failures and lessons. Removed 482 reviewed stale S1b identities; 11 surviving identifier/neutral-word matches retain their owners, steps and reasons. S1b debt is 1,192 → 721; total debt is 2,258 → 1,787. Unaffected entries and their order are unchanged. All 210 complete captured renders are identical, so no render baseline was refreshed. Test fixtures remain unchanged apart from the reviewed debt removals.

See [the turn 2 review](../../../reports/astra/s1b-non-runtime-wording-2026-09-27.md) for incorrect comments corrected, the remaining debt and validation.

## S1c review — 27 September 2026

Reviewed from `a1f33ef`: eight of 210 complete renders changed. Two settings-read failures now persist an empty owner-review hold instead of escaping; five verified portal captures and the portal action nudge capture gain the explicit unsupported-add rule. The other 202 captures are identical. Removed four exact obsolete practitioner-prompt debt entries (1,787 → 1,783); no new debt, reassignment or detector exemption. The five direct-helper/catalogue tests for the unreachable add capability are replaced in the pinned guard index by real dispatch, all-catalogue and false-success correction tests. Verified-register rows remain unchanged.

See [the S1c review](../../../reports/astra/s1c-redraft-2026-09-27.md) for the retained outcomes, exact review and rig checks.

## Chase rung review — 29 September 2026

One render record changed: `job-invoice-14`. The 14-day chase purpose said "second reminder" even when no reminder had been sent (an early "Chase now"); with no prior reminder it now reads as the first one sent. The candidate diff is the purpose sentence only; the other 209 records are identical. The three `test_shared_source_debt_is_exact_and_owned` failures (static asset entries) were failing before this change and are untouched.

## Quote deposit wording review — 30 September 2026

Seven render records changed, all for one reason: the Concierge's `quote_create` description now says to leave `deposit_percent` empty to use the business's own default deposit (its invoice settings), because "the usual deposit" means that. Live on the rig the Concierge searched the knowledge base, found no deposit and asked the owner, although a 25% default was set. Review method: for each candidate, remove exactly that sentence (it occurs twice in the corrective-nudge record) and the record hash equals the previous baseline, so nothing else moved: `nudge-concierge_action`, `route-concierge-typed|voice|staff`, `concierge-typed|voice|task`. The other 203 records are identical.

## No-charge visit flag review — 30 September 2026

21 render records changed, all for one reason: the service `hold_booking` and `offer_slots` tools gained an optional boolean `no_charge`, which the agent sets only when the business's own rules or knowledge say the visit is free (for example a return visit to put right its own work under its guarantee). Live on the rig a guarantee call-back was told "no charge" in the reply while the booking carried the service's callout price and two stray quotes were auto-drafted. Review method: for each candidate, remove the `no_charge` property from the tool schemas and the record hash equals the previous baseline, so nothing else moved: `services|exact_times|stays_appointments|required_gate_proof|recurrence|hartwell` in their `-draft`, `-redraft` and `-dispatch` forms, `portal-required_gate_proof-verified`, `portal-stays_appointments-verified` and `nudge-waiting_list`. The remaining records are identical. The description carries no business or trade names.

## Already-done instruction review — 30 September 2026

Four render records changed, all for one reason: the gathering prompt's rule against re-asking a detail the customer already gave now also says never to tell them to do something this email shows they have already done. Watching the onboarding film's first enquiry: the customer wrote "we're in a permit parking zone, is that a problem?" and the draft answered "just let us know before the visit that it's a permit zone", the knowledge note's instruction repeated to someone who had just followed it. Review method: for each candidate, replace the new clause with the full stop it displaced and the record hash equals the previous baseline, so nothing else moved: `gather-draft`, `gather-redraft`, `shop_only-draft`, `shop_only-redraft` (the clause occurs twice in the `shop_only` records, once in the corrective-nudge replay). The other 206 records are identical. The clause carries no business or trade names.

## Voice-sample timing review — 30 September 2026

One render record changed: `model-voice-sample`. The voice step's sample reply (the "here's how I'll sound" preview) answers a canned "availability next week?" enquiry; its system prompt said to keep details generic "(e.g. offer to confirm availability)", and on the rig during the first-sign-up film it wrote "next week is looking reasonable at the moment", a timing claim with no diary, which rule 2c / verified row 85 forbids in real replies. The prompt now says the model has not seen the diary, must never say or imply when the business is free, and should say it will check the diary and come back. Review method: replace the new clause with the old parenthesis and the record hash equals the previous baseline for all three date variants, so nothing else moved. The other 209 records are identical. The clause carries no business or trade names.

## Missing-details refusal review — 1 October 2026 (owed since 30 September)

One render record changed: `outcome-missing`. Commit `faa86a3e` (30 Sep) added one sentence to the booking-details refusal, "In the reply say NOTHING about availability, days, times or slots: nothing is reserved until these details are in, and the times come next.", and refreshed no baseline; this record has failed since. It went unseen because the gate was run as `python tests/run_offline.py | tail`, whose exit status is `tail`'s, and reported as passing. Review method: remove exactly that sentence from the candidate and the record hash equals the previous baseline, so nothing else moved. The sentence carries no business or trade names. Run the gate so its OWN exit status is read.

## Setup-prompt wording review — 1 October 2026

Eight render records changed (`platform-interview|industry|voice|mailbox|knowledge|data|help` and `extractor-website`; each name is one record across the three clock variants) for six wording changes found in the first-walk and re-run, 1 Oct 2026. (1) The interview step asks about a website only while the `website` field is still empty. (2) The voice step says an 'always give X' or 'never say Y' rule said alongside the tone is held for the knowledge base and kept out of `tone`, with each how-to-write instruction on its own `voice_notes` line. (3) The booking-details field description says a row is a question the customer answers, a how-the-business-works rule is a knowledge note, and a detail for some customers only is optional. (4) The dependants field description says a gate is written only when the owner says a lapsed or missing date must stop the booking. (5) The same description adds the `dropdown: Option one, Option two` type. (6) The website reader no longer targets 4-12 notes: one note per topic, every distinct rule, price and policy kept, `MAX_ENTRIES` 20 to 30.

Review method (strip-the-clause): for each candidate, replace every new text with the text it displaced (or remove the inserted sentences) and the record hash equals the previous baseline, so nothing else moved. Refreshed, with the changes each contains: `platform-interview` (1, 3, 4, 5), `platform-voice` (2, 3, 4, 5), `platform-industry|mailbox|knowledge|data|help` (3, 4, 5), `extractor-website` (6). The other 202 records are identical. One debt entry changed key: `render/platform-interview/$[0].system[0].text` (line 14, the interview guidance) already carried a flagged word; its fingerprint covers the whole line, so it moved with change 1. Owner, step and reason are kept, and none of the six new texts is flagged on its own.

## Prices-include-VAT catalogue line — 1 October 2026

Seven render records changed, all for one reason: the Getting paid catalogue gained one field, `prices_include_vat`, with the question the assistant asks ("when you tell a customer a price, is VAT already in it?"). In round three of the first-walk runs a quote added VAT on top of prices the business states as final, and the assistant had said "prices include VAT" with no setting behind it. Review method: remove exactly that one catalogue line from each candidate and the record hash equals the previous baseline, so nothing else moved: `platform-data`, `platform-help`, `platform-industry`, `platform-interview`, `platform-knowledge`, `platform-mailbox`, `platform-voice`. The other 203 records are identical. The line carries no business or trade names.

## Help assistant and drafted replies — 1 October 2026

One render record changed (`platform-help`; one record across the three clock variants), for one added sentence in the help prompt's rule about the feature request / issue report card: "You cannot change, redraft or send a customer's reply from here: when the owner asks for a drafted reply to be changed, say so plainly and tell them to press the thumbs-down on that draft, which opens the place where it can be redrafted; never describe a report card as a reply, a draft or a correction." Round three, 1 Oct 2026 (F25): asked to correct a drafted reply, the help assistant said "I've drafted a corrected reply… press Submit" while its only card was an issue report, so the customer's draft never changed. Review method: remove exactly that sentence from the candidate's JSON text, re-run `guard.render_record(serial(...))`, and the hash equals the previous baseline, so nothing else moved. The other 209 records are identical. The sentence carries no business or trade names.

## A date that runs out is always "expires" — 1 October 2026

Seven render records changed, all for one reason: the dependants field description gained one sentence, "A date that runs out is ALWAYS 'expires', gated or not: reminding is what 'expires N' does, so a date the owner wants to be reminded about is 'expires N' with no gate, never 'confirm'." In round three of the first-walk runs the assistant, told only when NOT to gate, carded a date that should remind as "confirm each visit", so no reminder could ever fire, and kept it after the owner's correction. Review method: remove exactly that sentence from each candidate and the record hash equals the previous baseline: `platform-data`, `platform-help`, `platform-industry`, `platform-interview`, `platform-knowledge`, `platform-mailbox`, `platform-voice`. The other 203 records are identical. The sentence carries no business or trade names.

Run all four guard modules together, or the full suite, through `tests/run_offline.py`. The session coverage check deliberately fails if retained renders have not been exercised. There is no environment switch, skip marker or automatic baseline update mode.

```powershell
python tests/run_offline.py tests -q --tb=short --disable-warnings --basetemp=.cache/pytest-full
```

## Evidence

- `inventory.json`: base commit, frozen clock, 41 configuration names and 210 render names.
- `renders/*.json`: canonical JSON SHA-256 hashes and 160-character excerpts of model-boundary requests, schemas, dispatcher results, receipts/state and rendered UI. Every complete live render is hashed and scanned before comparison. Key order is normalised; prompt whitespace and every value remain covered. Tests call the real assembly and consumers; provider, database and other external I/O are replaced.
- `outcomes.json`: all 91 current verified-register table rows, named existing guards for E1/E2/E4/E5/E8, and the redraft-CRM/promise-autoclose historical evidence. This preserves current evidence; it does not claim a new live replay.
- `debt.json`: file, enclosing Python def/class or template block, kind, normalised SHA-256 fingerprint and occurrence index within that scope. These form identity. Line numbers and 100-character excerpts are display-only hints, never compared. Render JSON pointers identify their value scope. Reasons are interned once at the top of the JSON; each entry still has an owner, closure step and reason reference. An occurrence is not a distinct product defect. Some maintenance language is conservatively included.

The clock is 28 September 2026 at 09:00 BST. The Hartwell configuration is sanitised documented test data from the existing out-of-hours tests, including their service policy, hours and prices. It is not a fresh production export. The neutral unseen business is Meridian Archive. All 18 current `industry_lingo` seeds also pass through actual draft, redraft and dispatcher assembly.

Tenant-owned wording is permitted only when an exact tagged value comes from the explicit `PROVENANCE` read-boundary map in `tests/agnostic_harness.py`. The same wording elsewhere is still detected. Short service tags preserve the real service-name length limit; a test compares cleaned Hartwell policy with the original fixture. Do not add word, module, preset or marketing exclusions.

## Reviewing a deliberate change

Regenerating committed baselines or debt is **only for a reviewed fix**. Never regenerate to silence a failing gate. Unrelated line insertion/deletion and function movement do not require manifest updates. A changed leak produces a new key and a stale old key; adding a leak or removing it without updating debt fails.

1. Export full renders on the review base and on the proposed revision into separate local directories, using the real offline callers:

   ```powershell
   python tests/agnostic_review.py .cache/review-before
   # Run in the proposed revision, with a new output directory:
   python tests/agnostic_review.py .cache/review-after
   git diff --no-index -- .cache/review-before .cache/review-after
   ```

   The review command always runs the assertions and exits nonzero on changes. It exports all 210 complete actual renders and `debt-changes.json` with new/changed and stale keys, current locations and full new snippets. It never changes committed fixtures or debt. Output directories must be new and inside the worktree. An ordinary failing test also writes its full candidate under `.cache/agnostic-candidates`.
2. Inspect the full before/after render diff, the debt key diff and the relevant caller's source diff. Explain the intended outcome and affected verified behaviour. A model stub proves the request and execution path, not a model's reaction.
3. Only after review, replace the affected render records using `tests.agnostic_guard.render_record` on the exported JSON. Review each added, changed or removed debt entry individually; preserve its owner/closure step, and remove stale entries only for the approved removal or change. Full previous snippets are available from the base's source or exported render, not merely its trimmed excerpt.
4. Run all four guard modules and the full offline suite. Keep the planted source/template/industry/duplicate/f-string, stable-location and real-prompt mutation tests passing. Remove generated scratch files before handover.

The detector uses an explicit trade vocabulary across all textual runtime sources under `app`, Python strings/f-strings/concatenations/comments/docstrings and rendered values/keys. It cannot prove semantic neutrality of unfamiliar vocabulary. Extend the vocabulary and negative tests when a new family is found; review the resulting debt, rather than excluding a surface. CI runs both the guard and full offline suite unconditionally on pushes and pull requests.

## Owner-word interview — 4 October 2026

Only `platform-interview` changed in this continuation: the guidance now keeps
owner words rather than mapping a category, its industry schema is free text,
and the redundant industry_other field is removed. Replacing exactly that
guidance and restoring those two schema fields reproduced the previous record
hash and excerpt. Removed four exact stale debt entries (two source strings,
the rendered guidance and the removed enum value); added no exemptions.
Manual-picker and other platform records are unchanged by this continuation.

# PR merge review — 18 October 2026

The inherited source guard mismatch was reviewed before merging Knowledge/header/logo work.
The public capability prompt now says “another party's” authorization instead of listing
trade roles. This keeps the distinction between approving a message and approving work.
The real-caller export in `.cache/review-merge-110` matched every existing render record:
721 tests passed, with no new or stale render debt and no snapshot changes for this edit.

PR #109 had replaced three launcher markup/comment occurrences with one inline CSS
`-webkit-touch-callout` property. The three obsolete exact entries were removed. The
remaining exact property is recorded with its own browser-menu explanation, owner and
closure step; it is not a business callout rule. The detector, vocabulary and file scope
are unchanged. This is a review of that one CSS occurrence, not a word/file exemption.

## Website source capture — 18 October 2026

Only `extractor-website` changes: the prompt requests separate exact quotations
for each note clause, all contributing pages, material non-main qualifications,
and questions only for absent source detail. Source booking routes are preserved
instead of assuming that import authorises replacement. The sources schema adds the exact
note `claim`. Replacing precisely the system prompt and schema in the complete
candidate with the base values reproduces the previous record byte for byte.
The obsolete comment describing that booking-route substitution is removed with
its exact source-debt occurrence. No detector, vocabulary or scan scope changes.
The product evidence map now includes the capture/fidelity/evidence-template
sources and their guards. The full `platform-help` comparison changes only the
`content_version` and `definition_version` fields inside the retrieved catalogue
JSON; replacing those fields restores the base request exactly. Its visible
answer context, permissions, actions and selected articles are unchanged.
