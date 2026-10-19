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
