"""Fetch the neural-VAD browser assets into src/app/static/vad/ (voice-live).

The live voice overlay's music immunity runs Silero VAD in the browser
(src/app/static/voice-live.js). The ~17MB of model + ONNX runtime binaries are
never committed (src/app/static/vad/ is gitignored); the client falls back to the
jsdelivr CDN when this directory is empty, so running this script is an
optional self-hosting upgrade (faster first load, no third-party fetch), not a
requirement.

Versions are pinned to the donor's field-tested pairing (OurTab lockfile:
@ricky0123/vad-web 0.0.30 + onnxruntime-web 1.27.0) and MUST match the
constants in src/app/static/voice-live.js.

Run: python scripts/build/fetch_vad_assets.py
"""
from __future__ import annotations

import pathlib
import urllib.request

VAD = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.30/dist/"
ORT = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.27.0/dist/"

ASSETS = [
    (VAD, "bundle.min.js"),
    (VAD, "silero_vad_v5.onnx"),
    (VAD, "silero_vad_legacy.onnx"),
    (VAD, "vad.worklet.bundle.min.js"),
    (ORT, "ort.min.js"),
    (ORT, "ort-wasm-simd-threaded.wasm"),
    (ORT, "ort-wasm-simd-threaded.mjs"),
]

out = pathlib.Path(__file__).resolve().parents[2] / "src/app" / "static" / "vad"
out.mkdir(parents=True, exist_ok=True)
for base, name in ASSETS:
    dest = out / name
    print(f"  {name} …", end="", flush=True)
    with urllib.request.urlopen(base + name, timeout=120) as r:
        dest.write_bytes(r.read())
    print(f" {dest.stat().st_size:,} bytes")
print(f"vad assets: {len(ASSETS)} files -> {out}")
