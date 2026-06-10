#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
chmod +x "$DIR/start.sh"
exec "$DIR/start.sh"
