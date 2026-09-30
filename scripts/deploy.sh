#!/usr/bin/env bash
# Build ARM64 wheels locally, then update the Compose stack on Jetson.
set -euo pipefail
HOST="${JETSON:-jetson@192.168.40.247}"
SSH="ssh -i $HOME/.ssh/ZDKey -o BatchMode=yes -o LogLevel=ERROR"
cd "$(dirname "$0")/.."
WHEEL_DIR="$(mktemp -d)"
FFMPEG_DIR="$(mktemp -d)"
trap 'rm -rf "$WHEEL_DIR" "$FFMPEG_DIR"' EXIT
uv build --wheel --out-dir "$WHEEL_DIR"
uv export --format requirements.txt --no-dev --no-emit-project --no-hashes \
  --output-file "$WHEEL_DIR/requirements.txt"
uv run --with pip python -m pip download --only-binary=:all: \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.14 \
  --abi cp314 --dest "$WHEEL_DIR" --requirement "$WHEEL_DIR/requirements.txt"
rm "$WHEEL_DIR/requirements.txt"
FFMPEG_ARCHIVE="${XDG_CACHE_HOME:-$HOME/.cache}/jetson-brain-v2/ffmpeg-release-arm64-static.tar.xz"
FFMPEG_HASH=f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1
mkdir -p "$(dirname "$FFMPEG_ARCHIVE")"
if ! printf '%s  %s\n' "$FFMPEG_HASH" "$FFMPEG_ARCHIVE" | sha256sum -c --status; then
  curl -fL --retry 2 -o "$FFMPEG_ARCHIVE.tmp" \
    https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz
  printf '%s  %s\n' "$FFMPEG_HASH" "$FFMPEG_ARCHIVE.tmp" | sha256sum -c
  mv "$FFMPEG_ARCHIVE.tmp" "$FFMPEG_ARCHIVE"
fi
tar -xJf "$FFMPEG_ARCHIVE" --wildcards --strip-components=1 \
  -C "$FFMPEG_DIR" '*/ffmpeg' '*/ffprobe'
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude dist/ --exclude wheels/ --exclude vendor/ --exclude nats-data/ \
  --exclude config/ai.json --exclude config/camera.json --exclude config/focus.json --exclude recordings/ \
  -e "$SSH" ./ "$HOST:/home/jetson/jetson-brain-v2/"
rsync -az --delete -e "$SSH" "$WHEEL_DIR/" "$HOST:/home/jetson/jetson-brain-v2/wheels/"
rsync -az --delete -e "$SSH" "$FFMPEG_DIR/" "$HOST:/home/jetson/jetson-brain-v2/vendor/"
$SSH "$HOST" 'cd ~/jetson-brain-v2 && docker compose build control && docker compose up -d --no-build && docker compose ps'
