# Checking installed database functions

The application calls SQL functions that `db/migrations/*.sql` install. A migration that
was merged and deployed with the application but never applied leaves the application
calling an older function. The effect can look like an ordinary product fault: a Diary
setup card whose Accept all includes a booking-mode line was refused by the previous
`change_setup_card`, because the Diary allowlist in that version has no `booking_mode`.

Run the check after every deploy and before any release note says a migration is live.
Set `SUPABASE_DB_URL` to the database connection string, in an environment with
`requirements-dev.txt` installed, and run it from the repository root:

```bash
python scripts/ops/check_db_functions.py
```

It reads the latest migration that defines each function (rollback files are ignored)
and compares that body, ignoring whitespace, with the body installed in the database. The
session is read-only and runs one catalogue query. The connection string is never printed.
One line is printed per function, then a summary:

- `ok`: the installed body matches the latest migration.
- `differs (latest definition: <file>)`: it does not. When the installed body matches an
  older migration, the line says which, which means the later migration has not been applied.
- `missing`: no function of that name is installed. That is expected for a feature whose
  migration has deliberately not been released yet; confirm it is.
- `not checked (rewritten in place by <file>)`: a later migration edits the installed body
  with `replace()`, so no file holds its final text and it cannot be compared.

The exit status is 0 when every function matches, 1 when any differs or is missing, and 2
when the check could not run (no connection string, or the database could not be read).

The check does not apply anything. Applying a migration is a separate release decision
with its own steps; run the check again afterwards. It compares function bodies only, not
argument lists, grants or tables. Two migration files with the same date are ordered by
file name, so confirm the order from the release notes when one reports `differs`.
