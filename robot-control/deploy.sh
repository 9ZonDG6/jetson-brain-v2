#!/usr/bin/env bash
# Copy robot-control to the Jetson and (re)create the venv there.
set -euo pipefail
HOST="${JETSON:-jetson@192.168.40.247}"
SSH="ssh -i $HOME/.ssh/ZDKey -o LogLevel=ERROR"
cd "$(dirname "$0")"
rsync -az --delete --exclude .venv --exclude __pycache__ --exclude ai-config.json -e "$SSH" ./ "$HOST:/home/jetson/robot-control/"
$SSH "$HOST" 'cd ~/robot-control && ~/.local/bin/uv venv -q --allow-existing --python 3.14 .venv && ~/.local/bin/uv pip install -q --python .venv/bin/python -r requirements.txt && echo deployed'
