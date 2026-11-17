#!/usr/bin/env bash
# Record a short two-armband simulated session and show the summary.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="${1:-./sessions}"

uv run myopyc record \
  --seconds 5 \
  --simulate 2 \
  --flush-interval 1.0 \
  --output-dir "$OUT_DIR" \
  --session-name demo

SESSION=$(uv run myopyc sessions --output-dir "$OUT_DIR" | head -n 1)
if [[ -n "${SESSION}" ]]; then
  echo
  echo "reading back ${SESSION}"
  uv run python scripts/inspect_session.py "$SESSION" --arrays
fi
