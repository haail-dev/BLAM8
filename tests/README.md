# Offline regression tests

Run the suite from the repository root:

```sh
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

Node.js is needed for the JavaScript replays. The native Knowledge form regression
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
