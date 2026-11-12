#!/usr/bin/env bash
# start.sh -- run Ward. Creates the venv on first run, installs dependencies
# only when requirements.txt changed (so it works offline afterwards), then
# launches.
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

STAMP="venv/.installed-requirements.txt"
if ! cmp -s requirements.txt "$STAMP"; then
    echo "Installing dependencies..."
    venv/bin/python3 -m pip install --quiet -r requirements.txt
    cp requirements.txt "$STAMP"
fi

if [ ! -f ".env" ]; then
    echo "No .env found -- copying .env.example so Ward has one."
    echo "Ward runs fine without editing it -- reverse-image search just"
    echo "stays off until you add a free SerpApi key under Settings."
    cp .env.example .env
fi

echo
echo "Starting Ward -- it will open in your browser."
echo "Press Ctrl+C to stop Ward."
echo
venv/bin/python3 desktop.py
