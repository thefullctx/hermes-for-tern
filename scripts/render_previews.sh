#!/bin/sh
# Re-record the README scenes and render their images with Tern (headless) and ffmpeg.
set -eu
cd "$(dirname "$0")/.."
uv run python scripts/record_motion.py assets
out=work/previews
rm -rf "$out"
tern shot docs/entrance-demo.txt docs/demo.txt --out "$out" --size 1200x800 --theme dark
cp "$out/entrance-demo/startup-dark.png" assets/startup.png
cp "$out/demo/session-dark.png" assets/screenshot.png
gif() {
  ffmpeg -loglevel error -y -framerate 15 -pattern_type glob -i "$1" \
    -vf "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" \
    "$2"
}
gif "$out/entrance-demo/entrance-*-dark.png" assets/entrance.gif
gif "$out/demo/session-*-dark.png" assets/demo.gif
ls -la assets
