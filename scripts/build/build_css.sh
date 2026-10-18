#!/usr/bin/env bash
# Deploy-time Tailwind build (render.yaml buildCommand): compiles the app
# shell's CSS once per deploy instead of the Play CDN recompiling it in the
# browser on every page (the "navigation takes seconds" fix, Kris 2026-08-31).
#
# Render's python runtime has no node, so this uses the tailwindcss standalone
# binary — downloaded once into .cache/ (kept out of git) and reused across
# builds when the cache survives.
set -euo pipefail
cd "$(dirname "$0")/../.."

TW_VERSION="v3.4.17"
BIN=".cache/tailwindcss-${TW_VERSION}"

if [ ! -x "$BIN" ]; then
  mkdir -p .cache
  # GitHub answered a release download with a 504 once (deploy 32737a8,
  # 2026-09-21: "curl: (22) The requested URL returned error: 504" while the
  # .cache had been evicted). Retry with a pause rather than fail the deploy.
  curl -fsSL --retry 6 --retry-delay 5 --retry-all-errors -o "$BIN" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TW_VERSION}/tailwindcss-linux-x64"
  chmod +x "$BIN"
fi

# tw.css = the app shells; tw-site.css = the public site (base.html);
# tw-plain.css = admin sign-in, track, capture, partner, driver. The last two
# replaced the Play CDN script on those pages (security review 2026-09, M10).
for pair in "config/tailwind/tailwind.config.js:tw.css" "config/tailwind/tailwind.site.config.js:tw-site.css" "config/tailwind/tailwind.plain.config.js:tw-plain.css"; do
  cfg="${pair%%:*}"; out="src/app/static/${pair##*:}"
  "$BIN" -c "$cfg" -i src/app/static/src/tw-input.css -o "$out" --minify
  echo "built $out ($(wc -c < "$out") bytes)"
done
