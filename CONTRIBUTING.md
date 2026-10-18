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

Use `python tests/run_offline.py ...` for tests. Database changes should include a
rollback and disposable-database verification where the migration permits rollback.
Deployment and live data changes are separate from local source development.
