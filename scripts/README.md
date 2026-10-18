# Platform commands

Run these commands from the repository root with the development environment
activated (`python -m pip install -r requirements-dev.txt`). Python application
imports come from the installed `src/` packages, not from copies in this folder.

| Folder | Purpose |
| --- | --- |
| `build/` | CSS compiler, optional VAD asset fetch, deterministic logo assets |
| `ci/` | Build-request workflow notifications |
| `db/migrations/` | Explicit, individually reviewed database migrations |
| `db/` | Backfills and database backup commands |
| `knowledge/` | Provision the platform's knowledge content |
| `ops/` | Operational configuration, metrics, IndexNow and VAPID helpers |

Examples:

```sh
npm run build:css
python -m scripts.build.derive_blam8_particle_map --help
python -m scripts.db.backup_db --list
python -m scripts.db.migrations.apply_access_requests_migration
```

Database and knowledge commands can alter the configured database. They remain
manual release/operations commands; moving them does not run them. Some older
migration scripts perform work at import time, so do not import them to inspect
their contents or auto-discover them as plugins. Read their source and schema
changes before selecting an individual migration. Migration order is a release
decision, not alphabetical filename order.

One inherited gap is explicitly labelled in `db/migrations/apply_slotgrid_migration.py`:
its historical slot-block backfill helper was absent from the starting baseline.
The command applies its existing DDL only; a legacy data conversion requires a
separately reviewed backfill. This reorganisation does not invent or run one.

Root-relative `.env` and `db/` paths refer to the repository root. Backups and
other operational output belong outside tracked source. The knowledge scripts
provision platform content; they are not fictional business/demo generators.
