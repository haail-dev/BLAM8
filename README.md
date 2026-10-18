# BLAM8

BLAM8 is an AI front desk for service businesses. It connects business mailboxes,
drafts replies from the business's own settings and knowledge, and supports
scheduling, customer records, quotes, invoices, and an owner-facing Concierge.

The application uses Python 3.12, FastAPI, Jinja templates, Supabase/PostgreSQL,
and Tailwind. Runtime packages live in `src/app/` and `src/speech_service/`.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Fill in your own development configuration before starting the app.
python -m uvicorn app.main:app --app-dir src --reload
```

Use a development database and mailbox. The local app can perform configured
background work when started; the offline test runner disables that work.
The development install registers the `src/` packages in your virtual environment.
Run the documented maintenance commands from the repository root.

## Verification

Node.js is required by the JavaScript replay tests. For database integration
tests, put PostgreSQL's `initdb`, `pg_ctl`, and `postgres` binaries on `PATH`.
The tests create their own disposable database clusters.

```powershell
# Set this to the same PostgreSQL bin directory used for the other DB tests.
$env:OUTREACH_TEST_POSTGRES_BIN = 'C:\path\to\postgresql\bin'
python tests/run_offline.py tests -q --tb=short --basetemp=.cache/pytest-full
```

The runner does not read `.env`, strips service credentials from its environment,
and blocks outbound Python sockets. GitHub Actions runs the suite and requires
the designated database tests to execute without skips.

For a local CSS build, run `npm ci` followed by `npm run build:css`.
Live browser rigs, screenshot capture, demo-data generators, and filming tools
are kept outside this platform repository.

## Build and deployment

`render.yaml` installs the Python service from `src/` and runs `bash scripts/build/build_css.sh` to
generate the three Tailwind stylesheets. Secrets belong in the environment;
`.env.example` lists the configuration names. `db/` contains the schema and
migrations; `scripts/db/migrations/apply_*_migration.py` are database maintenance commands.
Run migrations as a deliberate release step, separately from tests.

## Repository scope

- `src/app/`: web application, templates, static assets and background jobs.
- `src/speech_service/`: independent speech worker runtime.
- `db/`: schema and versioned SQL migrations.
- `tests/`: offline application regression checks and their synthetic fixtures.
- `scripts/`: grouped build, CI, database, knowledge and operational commands.
- `config/tailwind/`: stylesheet build configuration.
- `deploy/speech/`: worker container definition and its separate dependencies.
- `docs/`: modules, architecture, operations, security and behaviour references.

See [the source layout](docs/source-layout.md), [commands](scripts/README.md),
and [test guide](tests/README.md). Keep application source inside `src/`,
commands inside `scripts/`, and generated files under ignored output folders.

This is a new source baseline derived from the original BLAutoM8 repository.
See [provenance](docs/baseline-provenance.json),
[repository boundaries](docs/repository-boundaries.md), and
[contribution workflow](CONTRIBUTING.md). Historical research, reports, creative
production projects, and shared assistant tooling remain in the original workspace.
