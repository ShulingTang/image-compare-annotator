#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[INFO] python3 not found."
  if command -v apt-get >/dev/null 2>&1; then
    echo "[INFO] Trying to install python3 via apt-get (may require sudo password)..."
    sudo apt-get update && sudo apt-get install -y python3
  elif command -v brew >/dev/null 2>&1; then
    echo "[INFO] Trying to install python3 via brew..."
    brew install python
  else
    echo "[ERROR] Please install Python 3 manually first."
    exit 1
  fi
fi

export ANNOTATOR_PORT="${ANNOTATOR_PORT:-8765}"
export ANNOTATOR_AUTO_OPEN="${ANNOTATOR_AUTO_OPEN:-1}"
python3 app.py
