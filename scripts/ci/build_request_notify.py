"""Tell BLAM8 a build-request stage happened (called from the workflows).

  python scripts/ci/build_request_notify.py <issue> <event> [--pr N] [--note TEXT] [--url URL]

Needs BLAM8_URL and BUILD_REQUEST_SECRET in the environment; with either unset it
exits 0 quietly so the workflows work before the hook is configured. Never fails
the build: a missed storyline update is not worth a red run."""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("event")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--note", default="")
    ap.add_argument("--url", default="")
    a = ap.parse_args()
    base, secret = os.environ.get("BLAM8_URL", ""), os.environ.get("BUILD_REQUEST_SECRET", "")
    if not base or not secret:
        print("notify: BLAM8_URL / BUILD_REQUEST_SECRET not set; skipping")
        return 0
    payload = {"issue": a.issue, "event": a.event, "note": a.note, "url": a.url}
    if a.pr:
        payload["pr"] = a.pr
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        base.rstrip("/") + "/hooks/build-request", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Build-Timestamp": ts,
                 "X-Build-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("notify:", r.status, a.event)
    except Exception as e:  # noqa: BLE001 — deliberately never fails the run
        print("notify failed:", e.__class__.__name__, e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
