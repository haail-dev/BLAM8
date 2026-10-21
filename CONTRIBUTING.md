# Working on BLAM8

Use this repository for ongoing application development after the baseline is
accepted. Create one branch per change and open a pull request into `main`.
Keep unrelated changes in separate PRs. Review the diff, run the relevant checks,
and require CI to pass before merging. Tag intentional releases after validation.

Retain useful regression tests. Keep exploratory output, local customer/test
records, recordings, credentials, dependencies, and caches out of commits.
Follow `docs/source-layout.md` for file placement. Do not add maintenance scripts
to the repository root. Install `requirements-dev.txt` after changing the Python
package configuration so local imports use the current `src/` layout.
General-purpose assistant tools and creative projects belong in separate projects.

Preserve the outcomes documented in `docs/verified-behaviours.md`. Business-specific
facts and language belong in business settings and applied presets. Missing detail,
a failed rule, an unverifiable fact, and an unsupported request are different outcomes.
Do not invent a business configuration value when the owner has not chosen one.

Keep code comments and docstrings useful to the next reader. A docstring should explain
the function's purpose, inputs, outputs and important boundaries. Record non-obvious
design reasoning where it helps maintainers, but describe the general case rather than
embedding a particular person's conversation or transcript. Put concrete examples of
expected behaviour in focused regression tests; use synthetic or minimally redacted
data. If the reason depends on a dated incident, summarize the relevant condition and
date without quoting private conversation unless the exact wording is necessary to
specify or reproduce the behaviour. PR descriptions and review notes are also suitable
places for fuller historical context.

Use `python tests/run_offline.py ...` for tests. Database changes should include a
rollback and disposable-database verification where the migration permits rollback.
Deployment and live data changes are separate from local source development.

## What CI does here

Continuous integration (CI) automatically checks a proposed change in a fresh
environment. GitHub Actions runs this repository's offline checks on pull
requests and pushes to `main`. A green result applies to the tested commit;
changing the code requires new checks.

The offline workflow runs the business-agnostic guard first, then verifies that
designated disposable-database tests actually execute, then runs the complete
offline suite (including speech protocol tests). Dependencies are cached; obsolete
runs for the same PR are cancelled and each job has a 30-minute timeout. Main
runs are allowed to finish. The focused checks intentionally overlap the full
suite to give early, clear failures and then test everything together.

For an ordinary change:

1. Start a feature branch from current `main` and make one coherent change.
2. Run the relevant local tests. Run the complete agnostic guard when changing
   business defaults, prompts, templates, configuration or booking behaviour.
   See [the test guide](tests/README.md#business-agnostic-guard).
3. Review `git diff` and `git status` before committing. Include the relevant
   regression tests and documentation; keep credentials and generated files out.
4. Open a PR explaining the problem, change and validation. Inspect the diff and
   checks before merging. A skipped, cancelled or unavailable run is not a pass.
5. Merge the reviewed change, then validate and tag an intentional release.
   Deployments and migrations are separate decisions. After a deploy, and before
   saying a migration is live, run `python scripts/ops/check_db_functions.py`
   ([database function check](docs/operations/database-function-check.md)).

If Actions minutes are unavailable, run the same commands locally and record the
commit and results in the PR. That is useful evidence, but does not make a missing
required GitHub check green. Resume checks before merging when they are required;
do not weaken the guard to work around billing. Actions usage is charged to the
repository owner (`haail-dev`), and the organisation billing page is the place to
check its allowance.

If a PR is merged without CI because minutes ran out, keep track of it for later
checks. Add the `needs-ci` label to the PR (create the label if it is missing) and
link its local evidence comment. When minutes return, run the offline workflow on
current `main`, which covers everything merged in the meantime, plus any labelled
PR that is still open. Remove each label only once a green run includes that
change; if the run fails, fix it in a new PR before more work lands on top.

The initial baseline history replacement was a one-off migration. Normal changes
add commits through PRs; do not rewrite shared `main` to tidy up ordinary work.

## Automated PR review evidence

The PR template asks for Purpose, Changes, Validation, and Risks and rollback.
After the reporting workflow is merged into `main`, GitHub Actions maintains one
"Automated review evidence" comment per open PR. It refreshes on PR changes and
when either configured CI workflow finishes. Actions > PR review report > Run
workflow refreshes reports manually, for example after resolving a temporary
unknown mergeability result. Every invocation refreshes all open PRs, including
after main advances, so reports can identify stale base commits.

The report follows the five review steps: description completeness, changed-file
scope, sensitive paths, CI evidence and the human merge decision. It checks the
latest pull-request run for both `offline-tests.yml` and
`review-automation-tests.yml`, matching the PR number, head SHA, base SHA and base
branch. The named jobs must complete successfully; skipped, neutral, cancelled,
pending, missing or older results do not count. Rerun CI after updating a stale
branch. A report is a snapshot, so compare its head/base hashes with the current
PR before relying on it.

This is a deterministic evidence report, not an AI code review, secret scanner or
approval. It flags changed snapshots and guard/CI code for human inspection but
cannot decide whether a change weakens a rule or solves the right problem.
"Evidence complete" still requires reviewing the actual diff and rollback plan.
The report job going green means publishing succeeded, not merge approval.
It never merges, approves, enables auto-merge, deploys or changes branch settings.
Adding a new mandatory CI workflow requires updating `WORKFLOWS` in
`scripts/ci/pr_review.cjs` and the reporting workflow's `workflow_run` triggers.

The reporting workflow has a write token solely to maintain its PR comments.
It executes the helper from the default branch, never PR code or downloaded PR
artifacts, and uses no application secrets or paid AI APIs. Its own tests run in
a separate read-only workflow against the PR code. Run those tests locally with:

```sh
node --test tests/ci/pr_review.test.cjs
```

GitHub Free private repositories cannot enforce this report as a branch-protection
gate. Follow the review process even if the merge button is enabled.
