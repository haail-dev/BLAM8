"""Submit the public BLAM8 pages to IndexNow (Bing/Edge, Yandex, Seznam, Naver).

IndexNow is push-based indexing: instead of waiting days/weeks for Bingbot to
crawl a brand-new domain, this tells it directly which URLs exist. Run once
after any deploy that changes the public pages:

    python scripts/ops/seo_indexnow_ping.py              # everything in the live sitemap
    python scripts/ops/seo_indexnow_ping.py /setups /setups/farriers   # just these

Or from GitHub: Actions → "IndexNow ping" → Run workflow
(.github/workflows/indexnow.yml runs this script on a runner that can reach
blam8.com — handy when the machine you're on can't).

The URL list comes from the LIVE https://blam8.com/sitemap.xml, which src/app/main.py
renders from _PUBLIC_PAGES — so a page added there is submitted automatically,
and a page that isn't deployed yet can never be submitted by mistake. No app
import, no Supabase/env needed: only httpx.

The key must match INDEXNOW_KEY in src/app/main.py — the engines fetch
https://blam8.com/<key>.txt to prove we own the host, so the app must be
deployed with the key route live BEFORE this script is run.

IndexNow does not reach Google. For Google, resubmit the sitemap (or "Request
indexing" on a URL) in Search Console.
"""
import re
import sys

import httpx

HOST = "blam8.com"
BASE = f"https://{HOST}"
KEY = "8f3c1a5e9b2d47c6a0e4f7b8d1c52a39"  # keep in sync with src/app/main.py


def live_sitemap_urls() -> list[str]:
    r = httpx.get(f"{BASE}/sitemap.xml", timeout=20, follow_redirects=True)
    r.raise_for_status()
    urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
    if not urls:
        raise SystemExit(f"ABORT: {BASE}/sitemap.xml returned no <loc> entries.")
    return urls


def main(argv: list[str]) -> int:
    # sanity: the key file must be live, or the submission is silently ignored
    r = httpx.get(f"{BASE}/{KEY}.txt", timeout=20)
    if r.status_code != 200 or r.text.strip() != KEY:
        print(f"ABORT: {BASE}/{KEY}.txt not serving the key yet "
              f"(status {r.status_code}) — deploy first, then re-run.")
        return 1

    if argv:
        # explicit paths or full URLs on the command line
        urls = [a if a.startswith("http") else f"{BASE}{a if a.startswith('/') else '/' + a}"
                for a in argv]
    else:
        urls = live_sitemap_urls()

    print(f"Submitting {len(urls)} URL(s):")
    for u in urls:
        print(f"  {u}")

    payload = {"host": HOST, "key": KEY,
               "keyLocation": f"{BASE}/{KEY}.txt", "urlList": urls}
    resp = httpx.post("https://api.indexnow.org/indexnow", json=payload, timeout=30)
    # 200 = accepted, 202 = accepted-pending-key-check; anything else is a problem
    print(f"IndexNow: HTTP {resp.status_code} {resp.text.strip() or '(accepted)'}")
    return 0 if resp.status_code in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
