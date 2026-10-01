#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# extended_run.sh — Extended-duration Flask memory growth experiments  (Concern 6)
#
# Tests Flask under sustained load for longer durations (5+ minutes) to
# formally characterize memory growth behavior under different async
# configurations.
#
# Usage:
#   ./benchmarks/extended_run.sh [duration_seconds] [trials]
#
#   duration_seconds: how long to run each trial (default: 300 = 5 min)
#   trials:           number of trials per configuration (default: 3)
#
# Configurations tested:
#   1. Flask + eventlet (default, unpatched) — current behavior
#   2. Flask + eventlet (monkey-patched)
#   3. Flask + threading backend
#
# Output: benchmark_logs/extended_flask/
# ---------------------------------------------------------------------------
set -euo pipefail

DURATION="${1:-300}"
N_TRIALS="${2:-3}"
RATE=5000
BASE_DIR="benchmark_logs/extended_flask"

mkdir -p "$BASE_DIR"

echo "============================================"
echo " Extended Flask Memory Growth Experiment"
echo " Duration: ${DURATION}s per trial"
echo " Trials:   $N_TRIALS per config"
echo " Rate:     $RATE msg/s"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Helper: measure memory at 5-second intervals for long runs
# ---------------------------------------------------------------------------
measure_memory_extended() {
    local outfile="$1"
    local duration="$2"
    local samples=$((duration / 5))

    echo "elapsed_s,rss_kb,pss_kb,uss_kb" > "$outfile"

    for i in $(seq 1 "$samples"); do
        PIDS=$(pgrep -f "python.*app.py" 2>/dev/null || true)
        TOTAL_RSS=0; TOTAL_PSS=0; TOTAL_USS=0

        for pid in $PIDS; do
            if [ -d "/proc/$pid" ]; then
                local smaps="/proc/$pid/smaps_rollup"
                if [ -r "$smaps" ]; then
                    rss=$(awk '/^Rss:/ {sum += $2} END {print sum+0}' "$smaps")
                    pss=$(awk '/^Pss:/ {sum += $2} END {print sum+0}' "$smaps")
                    uss=$(awk '/^Private_/ {sum += $2} END {print sum+0}' "$smaps")
                    TOTAL_RSS=$((TOTAL_RSS + rss))
                    TOTAL_PSS=$((TOTAL_PSS + pss))
                    TOTAL_USS=$((TOTAL_USS + uss))
                fi
            fi
        done

        echo "$((i * 5)),$TOTAL_RSS,$TOTAL_PSS,$TOTAL_USS" >> "$outfile"
        sleep 5
    done
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
    pkill -f "python.*app.py" 2>/dev/null || true
    pkill -f "chrome" 2>/dev/null || true
    sleep 2
}

# ---------------------------------------------------------------------------
# Run one trial
# ---------------------------------------------------------------------------
run_trial() {
    local config_name="$1"
    local trial_num="$2"
    local app_script="$3"

    local trial_id="${config_name}_trial_${trial_num}"
    local trial_dir="$BASE_DIR/$trial_id"
    mkdir -p "$trial_dir"

    echo "--------------------------------------------"
    echo " $trial_id ($(date +%H:%M:%S))"
    echo "--------------------------------------------"

    cleanup

    # Start Flask backend
    cd example
    BENCHMARK_LOG_DIR="../$trial_dir" python3 "$app_script" &
    BACKEND_PID=$!
    cd ..
    sleep 3

    # Start memory measurement in background
    measure_memory_extended "$trial_dir/${config_name}_memory.csv" "$DURATION" &
    MEM_PID=$!

    # Run publisher for the full duration
    python3 flood_data.py \
        --rate "$RATE" \
        --duration "$DURATION" \
        --log-dir "$trial_dir" \
        --trial-id "$trial_id"

    # Wait for memory measurement
    wait "$MEM_PID" 2>/dev/null || true

    # Graceful shutdown
    kill -INT "$BACKEND_PID" 2>/dev/null || true
    for _wait in $(seq 1 50); do
        kill -0 "$BACKEND_PID" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill -9 "$BACKEND_PID" 2>/dev/null || true
    fi
    wait "$BACKEND_PID" 2>/dev/null || true

    # Extract final memory from CSV
    FINAL_MEM=$(tail -1 "$trial_dir/${config_name}_memory.csv" | cut -d, -f2)
    echo " ✓ $trial_id complete — Final RSS: $((FINAL_MEM / 1024)) MB"
    echo ""

    sleep 3
}

# ---------------------------------------------------------------------------
# Configuration 1: Default eventlet (unpatched)
# ---------------------------------------------------------------------------
echo ""
echo "=== Config 1: Default eventlet (unpatched) ==="
for t in $(seq 1 "$N_TRIALS"); do
    run_trial "eventlet_unpatched" "$t" "app.py"
done

# ---------------------------------------------------------------------------
# Configuration 2: Threading backend
# Create a temporary app variant that uses threading instead of eventlet
# ---------------------------------------------------------------------------
echo ""
echo "=== Config 2: Threading backend ==="

cat > example/app_threading.py << 'PYEOF'
"""Flask backend using threading instead of eventlet (Concern 6 experiment)."""
import sys
sys.path.insert(0, '.')
from app import app, socketio, mqtt, dump_stats
import signal, atexit

def _handler(sig, frame):
    dump_stats()
    sys.exit(0)

signal.signal(signal.SIGINT, _handler)
signal.signal(signal.SIGTERM, _handler)
atexit.register(dump_stats)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000,
                 use_reloader=False, debug=False,
                 async_mode='threading')
PYEOF

for t in $(seq 1 "$N_TRIALS"); do
    run_trial "threading" "$t" "app_threading.py"
done

# Clean up temp file
rm -f example/app_threading.py

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "============================================"
echo " Extended run complete!"
echo " Results: $BASE_DIR/"
echo ""
echo " To analyze memory growth:"
echo "   Look for RSS growth slope in *_memory.csv files"
echo "   Plot column 2 (rss_kb) vs column 1 (elapsed_s)"
echo "============================================"
