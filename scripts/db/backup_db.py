"""Timestamped pg_dump of the BLAM8 Supabase database to a local folder.

Why this exists: on 2026-08-19 the Supabase project auto-paused and the restore
was gated behind an unpaid invoice (402). The only copy of the data was inside
the account we could not reach. This script makes sure that is never true again.

Connection matches the scripts/db/migrations/apply_*_migration.py convention: session pooler,
DB_PASSWORD out of .env, project ref parsed from SUPABASE_URL.

Usage
    python scripts/db/backup_db.py              # dump, verify, prune
    python scripts/db/backup_db.py --list       # show what is already on disk
    python scripts/db/backup_db.py --out D:\\bk  # override the destination

Prerequisites
    * DB_PASSWORD set (uncommented, non-empty) in .env - Supabase dashboard ->
      Project Settings -> Database -> Database password.
    * pg_dump on PATH, or at one of the PG_DUMP_FALLBACKS below. Its major
      version must be >= the server's (currently PG 17).
    * The project must be ACTIVE. A paused project's host stops resolving; the
      script says so plainly rather than writing a truncated file.
"""
import argparse
import gzip
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_OUT = Path(os.environ.get("BLAM8_BACKUP_DIR", r"Z:\Backups\BLAM8"))
POOLER_HOST = os.environ.get("BLAM8_POOLER_HOST", "aws-1-eu-west-2.pooler.supabase.com")
POOLER_PORT = "5432"          # session mode - transaction mode (6543) cannot serve pg_dump
SCHEMAS = ("public", "auth")  # public = the business data, auth = the 9 sign-in identities
KEEP = int(os.environ.get("BLAM8_BACKUP_KEEP", "7"))

PG_DUMP_FALLBACKS = (
    r"Z:\Tools\pgsql\bin\pg_dump.exe",
    r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
    r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe",
)

# A dump that connects but silently returns nothing is worse than no dump at
# all, because it looks like success. These must appear with data.
SENTINEL_TABLES = ("tenants", "bookings", "crm_contacts")


def die(msg: str, hint: str = "") -> None:
    print(f"\nFAILED: {msg}", file=sys.stderr)
    if hint:
        print(f"  -> {hint}", file=sys.stderr)
    sys.exit(1)


def env_value(key: str) -> str:
    """Read key from .env. Unlike the migration scripts' helper this does NOT
    strip a leading '#': a commented-out key must read as absent, not as ''."""
    if not ENV_PATH.exists():
        die(f"{ENV_PATH} not found")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def find_pg_dump() -> str:
    found = shutil.which("pg_dump") or next(
        (p for p in PG_DUMP_FALLBACKS if Path(p).exists()), None
    )
    if not found:
        die("pg_dump not found",
            "install the PostgreSQL client tools, or add the path to PG_DUMP_FALLBACKS")
    return found


def list_backups(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("blam8-*.sql.gz"))


def verify(path: Path) -> None:
    """Re-read what we just wrote. Trusting pg_dump's exit code alone is how you
    end up with a folder full of 20-byte files you discover during an outage."""
    tables, copied = set(), {}
    current = None
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("CREATE TABLE "):
                    tables.add(line.split()[2].strip("(").split(".")[-1].strip('"'))
                elif line.startswith("COPY "):
                    current = line.split()[1].split(".")[-1].strip('"')
                    copied.setdefault(current, 0)
                elif current is not None:
                    if line.rstrip("\n") == "\\.":
                        current = None
                    else:
                        copied[current] += 1
    except (OSError, EOFError) as exc:
        die(f"backup is unreadable - {exc}", "the dump is truncated; delete it and re-run")

    if not tables:
        die("backup contains no tables", "the dump connected but returned nothing")

    empty = [t for t in SENTINEL_TABLES if copied.get(t, 0) == 0]
    print(f"  verified: {len(tables)} tables, "
          f"{sum(copied.values()):,} data rows, {path.stat().st_size / 1e6:.1f} MB")
    for name in SENTINEL_TABLES:
        print(f"    {name}: {copied.get(name, 0):,} rows")
    if empty:
        print(f"  WARNING: expected data in {', '.join(empty)} but found none. "
              f"Keeping the file, but check it before relying on it.", file=sys.stderr)


def prune(out_dir: Path) -> None:
    stale = list_backups(out_dir)[:-KEEP] if KEEP > 0 else []
    for old in stale:
        old.unlink()
        print(f"  pruned {old.name}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="destination folder")
    ap.add_argument("--list", action="store_true", help="list existing backups and exit")
    args = ap.parse_args()

    out_dir: Path = args.out
    if args.list:
        out_dir.mkdir(parents=True, exist_ok=True)
        found = list_backups(out_dir)
        print(f"{len(found)} backup(s) in {out_dir}")
        for f in found:
            print(f"  {f.name}  {f.stat().st_size / 1e6:6.1f} MB  "
                  f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}")
        return

    password = env_value("DB_PASSWORD")
    if not password:
        die("DB_PASSWORD is missing or commented out in .env",
            "Supabase dashboard -> Project Settings -> Database -> Database password, "
            "then add a DB_PASSWORD=... line (no leading '#')")

    supabase_url = env_value("SUPABASE_URL")
    match = re.search(r"https://([a-z0-9]+)\.supabase\.co", supabase_url)
    if not match:
        die(f"could not parse a project ref out of SUPABASE_URL ({supabase_url!r})")
    ref = match.group(1)

    # Catch a paused project here so the failure reads as "your project is
    # paused" rather than a wall of libpq output. Probe the PROJECT host, not
    # POOLER_HOST: the pooler is a shared regional endpoint that keeps
    # resolving no matter what state this project is in. Supabase drops the
    # project's own DNS record when it pauses, which is the actual signal.
    try:
        socket.getaddrinfo(POOLER_HOST, 5432)
    except socket.gaierror:
        die(f"cannot resolve {POOLER_HOST}", "no DNS / no network")
    try:
        socket.getaddrinfo(f"db.{ref}.supabase.co", 5432)
    except socket.gaierror:
        die(f"project {ref} looks paused (its DNS record is gone)",
            "resume it at "
            f"https://supabase.com/dashboard/project/{ref} - note that restore is "
            "blocked while an invoice is outstanding")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"blam8-{stamp}.sql.gz"

    cmd = [
        find_pg_dump(),
        "--host", POOLER_HOST,
        "--port", POOLER_PORT,
        "--username", f"postgres.{ref}",
        "--dbname", "postgres",
        "--no-owner", "--no-privileges",   # restore into a fresh project without role juggling
        "--format=plain",                  # plain + gzip stays greppable
    ]
    for schema in SCHEMAS:
        cmd += ["--schema", schema]

    env = {**os.environ, "PGPASSWORD": password, "PGSSLMODE": "require"}
    print(f"dumping {ref} ({', '.join(SCHEMAS)}) -> {target.name}")

    with gzip.open(target, "wb") as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        shutil.copyfileobj(proc.stdout, out)
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait()

    if code != 0:
        target.unlink(missing_ok=True)
        hint = ""
        if "password authentication failed" in stderr.lower():
            hint = "DB_PASSWORD is wrong - reset it in the Supabase dashboard"
        elif "server version" in stderr.lower():
            hint = "local pg_dump is older than the server; install matching client tools"
        die(f"pg_dump exited {code}\n{stderr.strip()}", hint)
    if stderr.strip():
        print(f"  pg_dump notes: {stderr.strip()}")

    verify(target)
    prune(out_dir)
    print(f"\nOK: {target}")


if __name__ == "__main__":
    main()
