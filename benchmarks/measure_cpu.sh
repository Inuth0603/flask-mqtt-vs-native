#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# measure_cpu.sh — Whole-process-tree CPU sampling  (Concern 2)
#
# Measures total CPU% across the ENTIRE application stack, including the
# browser renderer, so that web and native architectures are compared with
# the same measurement scope.
#
# Usage:
#   ./measure_cpu.sh <mode> [duration_seconds]
#
#   mode:
#     native   — measure the GTK4 dashboard process
#     node     — measure node server.js + all chromium children
#     flask    — measure python app.py  + all chromium children
#
#   duration_seconds: sampling window (default: 60)
#
# Output: benchmark_logs/<mode>_cpu.csv   (timestamp, cpu_total%)
# ---------------------------------------------------------------------------
set -euo pipefail

MODE="${1:?Usage: $0 <native|node|flask> [duration]}"
DURATION="${2:-60}"
LOG_DIR="${BENCHMARK_LOG_DIR:-benchmark_logs}"
mkdir -p "$LOG_DIR"

OUTFILE="$LOG_DIR/${MODE}_cpu.csv"
echo "elapsed_s,cpu_total_pct" > "$OUTFILE"

# Collect PIDs for the target process tree
get_pids() {
    case "$MODE" in
        native)
            pgrep -f "dashboard" || true
            ;;
        node)
            # Node backend
            pgrep -f "node server.js" || true
            # All Chromium processes for localhost:5001
            pgrep -f "chromium.*localhost:5001" 2>/dev/null || \
            pgrep -f "chrome.*localhost:5001"   2>/dev/null || \
            pgrep -f "chromium-browser"         2>/dev/null || \
            pgrep -f "google-chrome"            2>/dev/null || true
            ;;
        flask)
            # Python backend
            pgrep -f "python.*app.py" || true
            # All Chromium processes for localhost:5000
            pgrep -f "chromium.*localhost:5000" 2>/dev/null || \
            pgrep -f "chrome.*localhost:5000"   2>/dev/null || \
            pgrep -f "chromium-browser"         2>/dev/null || \
            pgrep -f "google-chrome"            2>/dev/null || true
            ;;
        *)
            echo "Unknown mode: $MODE" >&2
            exit 1
            ;;
    esac
}

echo "[measure_cpu] Mode=$MODE, Duration=${DURATION}s, Output=$OUTFILE"
echo "[measure_cpu] Waiting 2s for processes to stabilize..."
sleep 2

START=$(date +%s%N)
for i in $(seq 1 "$DURATION"); do
    PIDS=$(get_pids | tr '\n' ',')
    PIDS="${PIDS%,}"  # strip trailing comma

    if [ -z "$PIDS" ]; then
        echo "$i,0.0" >> "$OUTFILE"
    else
        # Sum %CPU across all matching PIDs
        TOTAL_CPU=$(ps -p "$PIDS" -o %cpu --no-headers 2>/dev/null \
                    | awk '{sum += $1} END {printf "%.1f", sum}')
        echo "$i,$TOTAL_CPU" >> "$OUTFILE"
    fi
    sleep 1
done

echo "[measure_cpu] Done. Results in $OUTFILE"

# Compute average
AVG=$(awk -F',' 'NR>1 {sum+=$2; n++} END {printf "%.1f", sum/n}' "$OUTFILE")
echo "[measure_cpu] Average total CPU%: $AVG"
