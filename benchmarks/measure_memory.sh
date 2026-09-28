#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# measure_memory.sh — PSS / USS memory measurement  (Concern 3)
#
# Uses /proc/[pid]/smaps_rollup to report Proportional Set Size (PSS) and
# Unique Set Size (USS) instead of RSS, avoiding double-counting of shared
# pages in Chromium's multi-process architecture.
#
# Usage:
#   ./measure_memory.sh <mode> [duration_seconds]
#
#   mode:
#     native   — measure the GTK4 dashboard process
#     node     — measure node server.js + all chromium children
#     flask    — measure python app.py  + all chromium children
#
#   duration_seconds: sampling window (default: 60)
#
# Output: benchmark_logs/<mode>_memory.csv   (timestamp, rss_kb, pss_kb, uss_kb)
# ---------------------------------------------------------------------------
set -euo pipefail

MODE="${1:?Usage: $0 <native|node|flask> [duration]}"
DURATION="${2:-60}"
LOG_DIR="${BENCHMARK_LOG_DIR:-benchmark_logs}"
mkdir -p "$LOG_DIR"

OUTFILE="$LOG_DIR/${MODE}_memory.csv"
echo "elapsed_s,rss_kb,pss_kb,uss_kb" > "$OUTFILE"

# Collect PIDs for the target process tree
get_pids() {
    case "$MODE" in
        native)
            pgrep -f "dashboard" || true
            ;;
        node)
            pgrep -f "node server.js" || true
            pgrep -f "chromium" 2>/dev/null || \
            pgrep -f "google-chrome" 2>/dev/null || true
            ;;
        flask)
            pgrep -f "python.*app.py" || true
            pgrep -f "chromium" 2>/dev/null || \
            pgrep -f "google-chrome" 2>/dev/null || true
            ;;
        *)
            echo "Unknown mode: $MODE" >&2
            exit 1
            ;;
    esac
}

# Read memory metrics from /proc/<pid>/smaps_rollup
# Returns: rss_kb pss_kb uss_kb  for a single PID
read_smaps() {
    local pid=$1
    local smaps="/proc/$pid/smaps_rollup"

    if [ ! -r "$smaps" ]; then
        echo "0 0 0"
        return
    fi

    local rss pss uss
    rss=$(awk '/^Rss:/ {sum += $2} END {print sum+0}' "$smaps")
    pss=$(awk '/^Pss:/ {sum += $2} END {print sum+0}' "$smaps")
    uss=$(awk '/^Private_/ {sum += $2} END {print sum+0}' "$smaps")
    echo "$rss $pss $uss"
}

echo "[measure_memory] Mode=$MODE, Duration=${DURATION}s, Output=$OUTFILE"
echo "[measure_memory] Waiting 2s for processes to stabilize..."
sleep 2

for i in $(seq 1 "$DURATION"); do
    PIDS=$(get_pids)

    TOTAL_RSS=0
    TOTAL_PSS=0
    TOTAL_USS=0

    for pid in $PIDS; do
        if [ -d "/proc/$pid" ]; then
            read -r rss pss uss <<< "$(read_smaps "$pid")"
            TOTAL_RSS=$((TOTAL_RSS + rss))
            TOTAL_PSS=$((TOTAL_PSS + pss))
            TOTAL_USS=$((TOTAL_USS + uss))
        fi
    done

    echo "$i,$TOTAL_RSS,$TOTAL_PSS,$TOTAL_USS" >> "$OUTFILE"
    sleep 1
done

echo "[measure_memory] Done. Results in $OUTFILE"

# Compute averages
awk -F',' 'NR>1 {
    rss+=$2; pss+=$3; uss+=$4; n++
} END {
    printf "[measure_memory] Averages — RSS: %.1f MB, PSS: %.1f MB, USS: %.1f MB\n",
        rss/n/1024, pss/n/1024, uss/n/1024
}' "$OUTFILE"
