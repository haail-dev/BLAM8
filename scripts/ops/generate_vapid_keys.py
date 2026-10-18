"""Generate a VAPID keypair for Web Push (PWA notifications) — run ONCE.

Prints the pair in the same base64url-raw format `npx web-push generate-vapid-keys`
produces, which is what pywebpush expects in VAPID_PRIVATE_KEY and what the
browser's pushManager.subscribe() expects as the applicationServerKey.

The SAME pair must be used everywhere (local .env + Render): existing browser
subscriptions are bound to the public key, so rotating it silently orphans them.

Run: python scripts/ops/generate_vapid_keys.py
Then set VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT in .env and on Render.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    print("VAPID_PUBLIC_KEY=" + b64u(public_raw))
    print("VAPID_PRIVATE_KEY=" + b64u(private_raw))
    print("VAPID_SUBJECT=mailto:hello@blam8.com")


if __name__ == "__main__":
    main()
