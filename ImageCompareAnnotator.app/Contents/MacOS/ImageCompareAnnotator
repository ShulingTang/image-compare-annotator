#!/usr/bin/env bash
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
export ANNOTATOR_PORT="${ANNOTATOR_PORT:-8765}"
export ANNOTATOR_AUTO_OPEN="${ANNOTATOR_AUTO_OPEN:-1}"
exec "$APP_DIR/start.sh"
