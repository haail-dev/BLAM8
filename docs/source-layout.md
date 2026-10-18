# Source layout

```text
BLAM8/
  src/
    app/                   Web application, jobs, templates and static source
    speech_service/        Independent speech worker Python package
  scripts/
    build/                 CSS and browser asset preparation
    ci/                    Workflow helpers
    db/migrations/         Explicit schema maintenance commands
    db/                    Backfills and backups
    knowledge/             Platform knowledge provisioning
    ops/                   Operational commands
  db/                      Canonical schema and SQL migration/rollback pairs
  config/tailwind/          Three stylesheet configurations
  deploy/speech/           Worker Dockerfile and dependency requirements
  tests/
    fixtures/              Synthetic data and reviewed guard records
    replays/               Offline JavaScript event replays
    speech/                Worker protocol regression tests
  docs/
    architecture/          Cross-feature designs
    modules/               Feature specifications
    operations/            Setup and operating references
    security/              Security and compliance decisions
    os/                    Supported operating-system shapes
```

The root contains project entry points and configuration recognised by the
toolchain: README, contributor instructions, Python and npm manifests,
requirements, the Render Blueprint, and Git configuration. It contains no
standalone maintenance Python scripts.

The Python packages keep their existing names: `from app import db` and
`from speech_service import server` still work after installing the project.
This is a file-layout refactor; it does not rename API routes, database tables,
business concepts, or services. Application modules remain together so existing
internal imports and monkeypatch-based regression checks retain their meaning.
Domain-level redesign can be reviewed separately from relocating the source.

Templates, static assets and the setup catalogue JSON stay inside the app
package. Runtime lookups resolve beside the package's `__file__`. Tailwind,
asset commands and tests resolve the new source paths. The Render start command
uses `--app-dir src`; local development also supports the editable install.

Build the speech image from the repository root:

```sh
docker build -f deploy/speech/Dockerfile -t blam8-speech .
```

Its Dockerfile-specific ignore file limits the context to worker source and
dependencies. It does not copy the web application, local configuration, models,
tests, or virtual environment. Worker deployment is still a separate operation.

Database scripts remain explicit operations. See `../scripts/README.md` before
running them. The cleanup moves their source; it does not apply migrations,
seed a database, change a live deployment, or publish a release.

Ordinary regression tests and synthetic fixtures stay with the platform.
Live browser rigs, screenshots, demo business generation and creative tooling
remain outside it. Generated CSS, Python bytecode, npm dependencies, virtual
environments and local test output are ignored by Git.

The local migration audit records each old and new path. The original mixed
workspace is preserved independently. Publishing this revised baseline and
replacing the organisation repository's initial history are separate steps.
