#!/usr/bin/env bash
# Copy the single v2 project to the Jetson and sync its environment.
set -euo pipefail
HOST="${JETSON:-jetson@192.168.40.247}"
SSH="ssh -i $HOME/.ssh/ZDKey -o LogLevel=ERROR"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude config/ai.json --exclude config/camera.json --exclude recordings/ \
  -e "$SSH" ./ "$HOST:/home/jetson/jetson-brain-v2/"
$SSH "$HOST" 'cd ~/jetson-brain-v2 && ~/.local/bin/uv sync --frozen && echo deployed'
