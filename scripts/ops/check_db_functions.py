"""Compare the database functions installed in a database with the migrations in this repository.

For every `create or replace function` in `db/migrations/*.sql` (rollback files excluded)
the body in the latest migration that defines it is compared, whitespace-normalised, with
the body installed in the database. One line is printed per function:

    <function>: ok
    <function>: differs (latest definition: <migration file>)
    <function>: missing
    <function>: not checked (rewritten in place by <migration file>)

A migration that was merged and deployed with the application but never applied shows up
as `differs` or `missing`. Run it after every deploy and before any release note says a
migration is live. Exit status is 0 when every function matches, 1 when any differs or is
missing, and 2 when the check could not run.

Read-only: the session is opened read-only and runs one catalogue query. It reads the
connection string from the SUPABASE_DB_URL environment variable and never prints it.
Needs `psycopg` (installed by requirements-dev.txt):

    python scripts/ops/check_db_functions.py
    python scripts/ops/check_db_functions.py --migrations path/to/migrations
"""
import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
ENV_NAME = "SUPABASE_DB_URL"
HEADER = re.compile(r'create\s+or\s+replace\s+function\s+((?:"?\w+"?\s*\.\s*)?"?\w+"?)\s*\(', re.I)
QUOTE = re.compile(r"\$(\w*)\$")


def normalise(text):
    return re.sub(r"\s+", " ", text).strip()


def _key(raw):
    parts = [p.strip().strip('"').lower() for p in raw.split(".")]
    return (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])


def definitions(sql):
    """Yield ((schema, name), normalised body) for each function the SQL creates or replaces.

    Scanning resumes after each body, so function text that appears inside another body
    (a comment or a string) is not read as a second definition.
    """
    position = 0
    while True:
        header = HEADER.search(sql, position)
        if not header:
            return
        opener = QUOTE.search(sql, header.end())
        closer = sql.find(opener.group(0), opener.end()) if opener else -1
        if closer < 0:
            position = header.end()
            continue
        yield _key(header.group(1)), normalise(sql[opener.end():closer])
        position = closer + len(opener.group(0))


def load_migrations(directory):
    """(history, rewriters) for the migration files, rollbacks excluded.

    history is {(schema, name): [(file name, body), ...]} in migration order. Migrations are
    ordered by file name, which starts with the date; two files with the same date sort
    alphabetically, so the one named last is treated as the later definition.

    rewriters is {(schema, name): [file name, ...]}: files that name a function and read or
    edit an installed body (`prosrc`, `pg_get_functiondef`) instead of defining it. No file
    holds the final text of such a function, so it cannot be compared.
    """
    history, texts = {}, {}
    for path in sorted(Path(directory).glob("*.sql")):
        if path.name.endswith(".rollback.sql"):
            continue
        texts[path.name] = path.read_text(encoding="utf-8")
        for key, body in definitions(texts[path.name]):
            history.setdefault(key, []).append((path.name, body))
    rewriters = {}
    for key, versions in history.items():
        for name, text in texts.items():
            lowered = text.lower()
            reads_installed_body = "prosrc" in lowered or "pg_get_functiondef" in lowered
            if name > versions[-1][0] and reads_installed_body and re.search(rf"\b{re.escape(key[1])}\b", lowered):
                rewriters.setdefault(key, []).append(name)
    return history, rewriters


def compare(history, installed):
    """(state, detail) for one function: its migration history against the installed bodies.

    `installed` lists every installed overload's normalised body; any one matching the latest
    migration counts, because the migrations do not record argument types.
    """
    latest_file, latest_body = history[-1]
    if not installed:
        return "missing", ""
    if latest_body in installed:
        return "ok", ""
    detail = f"latest definition: {latest_file}"
    older = next((name for name, body in reversed(history[:-1]) if body in installed), None)
    if older:
        detail += f"; installed body matches the older definition in {older}"
    return "differs", detail


def installed_bodies(connection, keys):
    """{(schema, name): [normalised body, ...]} read from the catalogue; read-only."""
    schemas = sorted({schema for schema, _ in keys})
    names = sorted({name for _, name in keys})
    rows = connection.execute(
        "select n.nspname, p.proname, p.prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where p.prokind = 'f' and n.nspname = any(%s) and p.proname = any(%s)", (schemas, names)).fetchall()
    found = {}
    for schema, name, source in rows:
        found.setdefault((schema, name), []).append(normalise(source or ""))
    return found


def report(history, installed, rewriters=None):
    """(lines, number of functions that differ or are missing, number not checked)."""
    lines, failures, unchecked = [], 0, 0
    for key in sorted(history):
        state, detail = compare(history[key], installed.get(key, []))
        if state == "differs" and (rewriters or {}).get(key):
            state, detail = "not checked", f"rewritten in place by {rewriters[key][-1]}"
            unchecked += 1
        label = key[1] if key[0] == "public" else ".".join(key)
        lines.append(f"{label}: {state}" + (f" ({detail})" if detail else ""))
        failures += state in ("differs", "missing")
    return lines, failures, unchecked


def _redact(message, secret):
    message = message.replace(secret, "<redacted>") if secret else message
    return re.sub(r"postgres(?:ql)?://\S+", "<redacted>", message)


def main(argv=None, environ=None, connect=None, out=None):
    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--migrations", default=str(ROOT / "db" / "migrations"),
                        help="directory of migration files (default: db/migrations)")
    args = parser.parse_args(argv)
    url = environ.get(ENV_NAME, "")
    if not url:
        print(f"{ENV_NAME} is not set; nothing was checked.", file=sys.stderr)
        return 2
    history, rewriters = load_migrations(args.migrations)
    if not history:
        print("No function definitions were found in the migrations; nothing was checked.", file=sys.stderr)
        return 2
    try:
        if connect is None:
            import psycopg
            connect = psycopg.connect
        with connect(url, connect_timeout=15) as connection:
            connection.read_only = True
            installed = installed_bodies(connection, list(history))
    except Exception as exc:
        print(f"Could not read the database functions: {type(exc).__name__}: {_redact(str(exc), url)}", file=sys.stderr)
        return 2
    lines, failures, unchecked = report(history, installed, rewriters)
    print("\n".join(lines), file=out)
    print(f"{len(lines) - failures - unchecked} of {len(lines)} functions match their latest migration"
          + (f"; {unchecked} not checked." if unchecked else "."), file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
