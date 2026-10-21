# Offline regression tests

Run the suite from the repository root:

```sh
npm ci --ignore-scripts
npm run build:css
python tests/run_offline.py tests -q --tb=short
```

The runner disables local `.env` loading and outbound Python sockets before
importing application code. Database tests use disposable local PostgreSQL
clusters. Set `OUTREACH_TEST_POSTGRES_BIN` to the directory containing the
PostgreSQL binaries if they are not on PATH.

- `test_*.py`: application behaviour and integration regressions.
- `fixtures/`: synthetic test inputs and reviewed agnostic-guard records.
- `replays/`: JavaScript fixtures for synthetic DOM replays and the native browser test.
- `speech/`: separate speech worker protocol tests using fake models.
- `agnostic_*.py` and other fixture helpers: shared test support.

Node.js 22+ is needed for the JavaScript replays. From a clean checkout, build
the three production stylesheets with the commands above before the full suite
or `test_onboarding_layout_browser.py`. Those generated CSS files are ignored;
the native layout checks read actual production CSS and fail when it is missing.
CI installs the lockfile dependencies and builds the same styles before its full
suite; no generated CSS is committed. The native Knowledge form regression
also requires Chromium/Chrome (available on the Ubuntu CI image). It discovers
`google-chrome`, `google-chrome-stable`, `chromium` or `chromium-browser` on PATH,
or a standard Windows Chrome installation. Set `BLAM8_TEST_CHROMIUM_BIN` to an
explicit executable if needed; a missing browser fails rather than skipping.
The test launches a separate headless process with a disposable profile and a
30-second timeout, opens only a local fixture with network access blocked, and
uses native DOM/FormData. Only its fetch response transport is substituted with
backend-generated fixtures; this is not a live backend/browser E2E test. No
Playwright installation or screenshot pipeline is required. Speech protocol tests
require the development dependencies, including NumPy; they download no speech models.

`test_knowledge_sync_startup_browser.py` serves a rendered Knowledge page only
on an ephemeral loopback listener, runs the full production Concierge, guide and
sync controllers in an isolated Chromium profile, and substitutes response
transport. Native timers must remain live after fixture HTTP409 responses; restored
source/overlap choices and the current unapproved note are checked with FormData.
Separate route/disposable-SQL tests verify automatic-view replay identity.

`test_knowledge_apply_sync_browser.py` runs the real source-correction routes
behind the sync wrapper for two native documents with disposable SQL and no
model. It applies a paragraph removal while an idle poll observes the shared
revision before the Apply response, then checks that the originating device
keeps its page and unsaved decisions, the checkpoint carries the applied draft
under the advanced version, and the second device reloads and recovers them.

The mailbox submit regression additionally drives trusted Chromium mouse input
through its loopback-only debugging endpoint with Node 22+ built-in fetch and
WebSocket APIs. It renders the shipped backlog form and runs the production sync
client, actual mailbox choice handler and disposable SQL replay protection. The
mailbox persistence boundary records synthetic choices; existing database tests
separately verify the mailbox choice RPC. It covers cancellation and native
validation followed by retry. Isolated profiles and bounded subprocess timeouts
apply; no Python network exception, new package or browser profile is required.

Test filenames stay stable because existing regression modules share fixtures
and CI selects certain database tests explicitly. Keep new generated evidence
under `.cache/`, not beside test source.

## Business-agnostic guard

Run all four modules together from the repository root:

```sh
python tests/run_offline.py tests/test_agnostic_context.py tests/test_agnostic_surfaces.py tests/test_agnostic_outcomes.py tests/test_agnostic_inventory.py -q --tb=short --basetemp=.cache/pytest-guard
```

The guard protects a product rule: business facts and policies come from that
business's configuration, rather than assumptions baked into shared code.
For example, changing the industry label must not silently change booking
permissions, pricing or the booking result.

It combines three checks:

- Scan supported text files throughout `src/`, including the speech worker and
  future packages, for a conservative list of industry-specific words. Tracked
  and untracked non-ignored source is covered; ignored generated output is not.
  Missing or empty source fails instead of silently producing a clean scan.
- Compare complete assembled prompts, tools and selected responses against
  reviewed snapshots, using synthetic businesses and fake model responses.
  Here, a "render" is assembled text/data, not a browser screenshot.
- Exercise booking outcomes and configuration boundaries: missing information,
  failed rules, unavailable verification and unsupported operations remain
  distinct, and one business's configuration cannot change another's decision
  in the covered scenarios.

`fixtures/agnostic/debt.json` records existing findings at exact locations and
contents. A passing guard means no unreviewed change relative to that register;
it does not mean zero existing findings. Words in comments, marketing copy and
explicit presets can also be findings. Review their context before calling them
bugs. The register's U/S identifiers refer to the historical review plan.

When the guard fails, inspect the changed source or candidate JSON under
`.cache/agnostic-candidates/`. For a complete local review export:

```sh
python tests/agnostic_review.py .cache/agnostic-review
```

Use a new output directory each time. This exports evidence and still returns a
failure if assertions fail; it never approves replacement snapshots. Fix an
unintended assumption in code. For an intentional behaviour change, explain the
change in the PR, review the complete old/new output, and update only the affected
records with a reason. Never bulk-refresh the register just to get a green check.

The vocabulary scan cannot understand every business assumption, and fake-model
tests cannot prove how a live AI model will respond. They complement the wider
regression suite and human review. The inventory module requires all retained
renders to execute, so running it alone is expected to fail coverage accounting.
