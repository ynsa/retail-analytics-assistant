#!/usr/bin/env bash
# Export the draw.io diagrams in docs/design/diagrams/ to PNG (spec 11, B2).
# The PNG files contain a copy of the diagram, so draw.io can open them as editable diagrams.
# Needs the draw.io desktop app: the `drawio` command, or the app in /Applications on macOS.
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v drawio >/dev/null 2>&1; then
  DRAWIO="$(command -v drawio)"
elif [ -x /Applications/draw.io.app/Contents/MacOS/draw.io ]; then
  DRAWIO=/Applications/draw.io.app/Contents/MacOS/draw.io
else
  echo "draw.io is not installed. Export by hand at app.diagrams.net (see spec 11, B2)." >&2
  exit 1
fi

for name in production-gcp prototype; do
  "$DRAWIO" --export --format png --scale 2 --border 20 --embed-diagram \
    --output "docs/design/diagrams/$name.png" "docs/design/diagrams/$name.drawio"
done
