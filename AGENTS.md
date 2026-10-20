# BLAM8 repository instructions

Read README.md and CONTRIBUTING.md. Keep the scope to this application, its build,
regression checks, and current product/operational documentation.

Follow docs/source-layout.md. Runtime packages belong in src/, maintenance
commands in scripts/, and configuration in config/ or deploy/. Preserve public
Python import names (app and speech_service) and HTTP routes when moving files.
Update build commands, workflow paths, documentation and tests together.

Preserve the business-agnostic behaviour and outcome rules in CONTRIBUTING.md and
docs/verified-behaviours.md. Keep app features intact during structural cleanup.
Use the offline runner for automated tests. Never copy local credentials or data
from the historical BLAutoM8 workspace into this repository.

Use a feature branch and a PR for ordinary changes once the initial baseline is
accepted. Keep shared agent bridges, creative production projects, run evidence,
and generated output outside the tracked app source. Local test scratch belongs
under .cache; use a separate directory for reports that need to be retained.

Default to one active PR at a time. Finish, review and merge the current PR
before opening the next overlapping change, then start that change from updated
main. Keep overlapping follow-up work local until then; a draft PR is not a way
to avoid CI runs. Use parallel PRs only when the user explicitly requests them.

Complete every relevant check that can reasonably run locally before pushing
or updating a PR. Run fast structural, product-evidence and coverage checks
first, then focused behavior and affected real-caller or snapshot tests, and
finish with diff and build checks where relevant. Resolve failures locally so
CI confirms a prepared change rather than serving as the first diagnostic.
Do not repeat broad local checks without a new change or failure that warrants
them; normal CI may still run the full suite.

The user gives standing permission to merge the active PR without asking again
once its exact current head has completed review, passed relevant local checks
and all required CI checks, and has no unresolved conflicts or review findings.
Use the normal GitHub API or CLI and guard the merge against an unexpected head
change. During active work, follow CI through completion and merge promptly when
these conditions are met, then verify GitHub reports the PR as merged before
starting the next overlapping change. Do not treat pending, failed or skipped
checks as success. This workflow does not depend on GitHub native auto-merge.
Keep explicitly held or draft PRs on hold until brought into the active workflow.
Deployment, migration and feature activation remain separate release actions.

For the initial migration, docs/baseline-provenance.json identifies the source
commit. Unfinished work in the historical workspace has not been imported wholesale.

Keep configuration with the area that owns it. Diary, calendar, scheduling,
availability and capacity share Diary settings. Team assignment configuration
belongs in People settings and is not a Diary onboarding requirement. Use the
existing compact expandable settings rows and business-neutral labels.

Keep Diary settings and Booking requirements as separate onboarding steps.
Diary settings owns booking mode, availability, timing and capacity; Booking
requirements owns customer answers, documents and their collection rules.
Saving the checklist must preserve the configured booking mode.
