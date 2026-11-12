#!/usr/bin/env bash
# start.sh -- double-click (or `bash start.sh`) to run Ward. Creates the
# venv on first run, installs/updates dependencies, then launches.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Install Python 3.10+ (python.org, or your"
    echo "system package manager), then run this again."
    exit 1
fi

if [ ! -x "venv/bin/python3" ]; then
    echo "Setting up Ward for the first time -- this only happens once..."
    python3 -m venv venv
fi

# Only touch the network when requirements.txt changed since the last
# successful install, so Ward starts offline after the first run.
REQ_HASH="$(venv/bin/python3 -c "import hashlib;print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())")"
if [ ! -f "venv/.requirements.sha256" ] || [ "$(cat venv/.requirements.sha256)" != "$REQ_HASH" ]; then
    echo "Installing dependencies..."
    venv/bin/python3 -m pip install --quiet --upgrade pip
    venv/bin/python3 -m pip install --quiet -r requirements.txt
    echo "$REQ_HASH" > venv/.requirements.sha256
fi

if [ ! -f ".env" ]; then
    echo "No .env found -- copying .env.example so Ward has one."
    echo "Ward runs fine without editing it -- reverse-image search just"
    echo "stays off until you add a free SerpApi key under Settings."
    cp .env.example .env
fi

echo
echo "Starting Ward -- it opens in your browser (address shown below)..."
echo "Press Ctrl+C to stop Ward."
echo
venv/bin/python3 desktop.py
