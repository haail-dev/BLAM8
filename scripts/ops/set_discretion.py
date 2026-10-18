"""Turn discretion mode on or off for one tenant — the filming mask.

    python scripts/ops/set_discretion.py --list
    python scripts/ops/set_discretion.py --tenant <uuid|name fragment> --on
    python scripts/ops/set_discretion.py --tenant Willowbrook --off

There is deliberately NO user interface for this. A real tenant should never
see the setting, never see the script it injects, and never know the mode
exists — so the only way to flip it is here, on purpose, by someone with the
service key.

What it does when on: src/app/main.py injects src/app/static/discretion.js, which masks
email addresses, phone numbers, postcodes and house numbers in the rendered
text. Nothing stored changes; only what is on screen.
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from app.db import db  # noqa: E402


def tenants() -> list[dict]:
    r = db().table("tenants").select("id,name").order("name").execute()
    return r.data or []


def resolve(needle: str) -> dict:
    rows = tenants()
    exact = [t for t in rows if t["id"] == needle]
    if exact:
        return exact[0]
    hits = [t for t in rows if needle.lower() in (t["name"] or "").lower()]
    if not hits:
        raise SystemExit(f"no tenant matching {needle!r}. Try --list.")
    if len(hits) > 1:
        raise SystemExit("ambiguous, matches:\n  "
                         + "\n  ".join(f"{t['id']}  {t['name']}" for t in hits))
    return hits[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="every tenant and its state")
    ap.add_argument("--tenant", default="", help="uuid, or part of the name")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    a = ap.parse_args()

    if a.list or not a.tenant:
        cfg = {c["tenant_id"]: c.get("discretion")
               for c in (db().table("tenant_config").select("tenant_id,discretion")
                         .execute().data or [])}
        for t in tenants():
            state = "ON " if cfg.get(t["id"]) else "   "
            print(f"  {state} {t['id']}  {t['name']}")
        print("\n  (ON = the app masks emails, phones, postcodes and house numbers"
              " on screen)")
        return

    t = resolve(a.tenant)
    if not (a.on or a.off):
        raise SystemExit("say --on or --off")
    want = bool(a.on)
    db().table("tenant_config").upsert({"tenant_id": t["id"], "discretion": want},
                                       on_conflict="tenant_id").execute()
    r = (db().table("tenant_config").select("discretion")
         .eq("tenant_id", t["id"]).limit(1).execute())
    now = bool((r.data or [{}])[0].get("discretion"))
    print(f"  {t['name']} ({t['id']})")
    print(f"  discretion: {'ON' if now else 'off'}")
    if now != want:
        raise SystemExit("  ! did not take — check the service key")
    print("\n  Live within a minute (src/app/discretion.py caches for 60s).")


if __name__ == "__main__":
    main()
