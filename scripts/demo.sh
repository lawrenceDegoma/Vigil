#!/usr/bin/env bash
# Generate a synthetic "day" with one convulsion-like episode, analyze it as a
# video file (plot + overlay), then run the synthetic evaluation set.
# Everything is written under runs/ and data/ (both gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."
VIGIL=(uv run --project services/edge vigil)

"${VIGIL[@]}" synth mixed -o data/synthetic/mixed.mp4
"${VIGIL[@]}" analyze data/synthetic/mixed.mp4 --plot --overlay -o runs/demo
"${VIGIL[@]}" evaluate --synthetic --json-out runs/demo/synthetic-eval.json
echo
echo "Open runs/demo/timeline.png and runs/demo/overlay.mp4 to inspect the analysis."
