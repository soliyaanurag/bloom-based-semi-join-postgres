#!/usr/bin/env bash
#
# run_all.sh: reruns every Batch 4 experiment and redraws all charts with
# one command:
#   1. run_experiments.py  (appends to results/results.csv, about 4 minutes)
#   2. b_sweep.py          (writes results/b_sweep.csv, about 1 minute)
#   3. plot.py             (writes the PNG charts and results/fp_rate.csv)
#
# Needs: the server running, both sites set up (setup/setup_all.sh), and
# the Python packages in .venv (see docs/WALKTHROUGH.md, Batch 4).
#
# Usage (from the project folder):
#     source setup/env.example.sh
#     bench/run_all.sh

# Stop at the first command that fails.
set -e

# The project folder (the parent of the folder this script is in).
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

echo "=== 1/3: run_experiments.py ==="
"$PYTHON" "$PROJECT_DIR/bench/run_experiments.py"

echo "=== 2/3: b_sweep.py ==="
"$PYTHON" "$PROJECT_DIR/bench/b_sweep.py"

echo "=== 3/3: plot.py ==="
"$PYTHON" "$PROJECT_DIR/bench/plot.py"
