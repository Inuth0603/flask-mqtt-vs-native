#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fps_sweep.sh — Clean FPS parameter sweep (Native vs Node.js)
#
# Measures CPU (process-tree) and Memory (PSS via smaps_rollup) for both
# Native GTK4 and Node.js+Canvas at 30, 60, and 144 FPS.
#
# Key improvements over manual measurement:
#   1. Chrome is launched with --app and a fresh --user-data-dir so NO other
#      tabs/extensions contaminate the numbers.
#   2. PIDs are tracked precisely by process group / known PID, not broad
#      pgrep patterns.
#   3. Everything is automated: ~10 minutes unattended.
#
# Usage:
#   ./benchmarks/fps_sweep.sh [duration_seconds]
#
#   duration: seconds per configuration (default: 60)
#
# Output: benchmark_logs/fps_sweep/summary.csv
# ---------------------------------------------------------------------------
set -euo pipefail

DURATION="${1:-60}"
RATE=5000
BASE_DIR="benchmark_logs/fps_sweep"
FPS_VALUES=(30 60 144)
ARCHITECTURES=("native" "node")

mkdir -p "$BASE_DIR"

# Detect Chrome binary
CHROME_CMD=""
if command -v google-chrome &>/dev/null; then
    CHROME_CMD="google-chrome"
elif command -v chromium-browser &>/dev/null; then
    CHROME_CMD="chromium-browser"
elif command -v chromium &>/dev/null; then
    CHROME_CMD="chromium"
else
    echo "[ERROR] No Chrome/Chromium binary found!"
    exit 1
fi

echo "============================================================"
echo "  FPS Parameter Sweep"
echo "  Architectures: ${ARCHITECTURES[*]}"
echo "  FPS values:    ${FPS_VALUES[*]}"
echo "  Duration:      ${DURATION}s per config at ${RATE} msg/s"
echo "  Chrome:        $CHROME_CMD"
echo "  Output:        $BASE_DIR/summary.csv"
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# Clean up any stale processes
# ---------------------------------------------------------------------------
cleanup_all() {
    pkill -9 -f "dashboard/build/dashboard" 2>/dev/null || true
    pkill -9 -f "node server.js" 2>/dev/null || true
    pkill -f "user-data-dir=/tmp/fps_sweep" 2>/dev/null || true
    sleep 2
}

cleanup_all

# ---------------------------------------------------------------------------
# PID-accurate resource measurement functions
# ---------------------------------------------------------------------------

# Get all child PIDs of a given parent PID (recursive)
get_descendants() {
    local parent=$1
    local children
    children=$(pgrep -P "$parent" 2>/dev/null || true)
    for child in $children; do
        echo "$child"
        get_descendants "$child"
    done
}

# Get all PIDs for a process tree rooted at $1, plus optional Chrome PID tree at $2
get_all_pids() {
    local backend_pid="${1:-}"
    local chrome_pid="${2:-}"

    if [ -n "$backend_pid" ] && kill -0 "$backend_pid" 2>/dev/null; then
        echo "$backend_pid"
        get_descendants "$backend_pid"
    fi

    if [ -n "$chrome_pid" ] && kill -0 "$chrome_pid" 2>/dev/null; then
        echo "$chrome_pid"
        get_descendants "$chrome_pid"
    fi
}

# Measure total CPU% for a set of PIDs
measure_cpu() {
    local pids="$1"
    if [ -z "$pids" ]; then
        echo "0.0"
        return
    fi
    local pid_csv
    pid_csv=$(echo "$pids" | tr '\n' ',' | sed 's/,$//')
    ps -p "$pid_csv" -o %cpu --no-headers 2>/dev/null \
        | awk '{sum += $1} END {printf "%.1f", sum}' || echo "0.0"
}

# Measure total PSS (kB) for a set of PIDs via smaps_rollup
measure_pss() {
    local pids="$1"
    local total_pss=0
    for pid in $pids; do
        local smaps="/proc/$pid/smaps_rollup"
        if [ -r "$smaps" ]; then
            local pss
            pss=$(awk '/^Pss:/ {sum += $2} END {print sum+0}' "$smaps" 2>/dev/null)
            total_pss=$((total_pss + pss))
        fi
    done
    echo "$total_pss"
}

# Measure total RSS (kB) for a set of PIDs
measure_rss() {
    local pids="$1"
    if [ -z "$pids" ]; then
        echo "0"
        return
    fi
    local pid_csv
    pid_csv=$(echo "$pids" | tr '\n' ',' | sed 's/,$//')
    ps -p "$pid_csv" -o rss --no-headers 2>/dev/null \
        | awk '{sum += $1} END {print sum+0}' || echo "0"
}

# ---------------------------------------------------------------------------
# Write CSV header
# ---------------------------------------------------------------------------
SUMMARY="$BASE_DIR/summary.csv"
echo "architecture,target_fps,elapsed_s,cpu_pct,pss_kb,rss_kb" > "$SUMMARY"

# ---------------------------------------------------------------------------
# Run each configuration
# ---------------------------------------------------------------------------
for ARCH in "${ARCHITECTURES[@]}"; do
  for TARGET_FPS in "${FPS_VALUES[@]}"; do
    CONFIG_ID="${ARCH}_${TARGET_FPS}fps"
    CONFIG_DIR="$BASE_DIR/$CONFIG_ID"
    mkdir -p "$CONFIG_DIR"

    echo "------------------------------------------------------------"
    echo "  $CONFIG_ID  ($(date +%H:%M:%S))"
    echo "------------------------------------------------------------"

    BACKEND_PID=""
    CHROME_PID=""
    CHROME_PROFILE=""

    # --- Start backend ---
    case "$ARCH" in
        native)
            echo "[sweep] Starting native dashboard (TARGET_FPS=$TARGET_FPS)..."
            TARGET_FPS="$TARGET_FPS" BENCHMARK_LOG_DIR="$CONFIG_DIR" \
                ./dashboard/build/dashboard &
            BACKEND_PID=$!
            sleep 3
            ;;
        node)
            echo "[sweep] Starting Node.js backend..."
            cd node_backend
            BENCHMARK_LOG_DIR="../$CONFIG_DIR" node server.js &
            BACKEND_PID=$!
            cd ..
            sleep 3

            # Launch Chrome in app mode with a FRESH, isolated profile
            CHROME_PROFILE=$(mktemp -d /tmp/fps_sweep_chrome_XXXXXX)
            echo "[sweep] Launching Chrome (fps=$TARGET_FPS, profile=$CHROME_PROFILE)..."
            $CHROME_CMD \
                --user-data-dir="$CHROME_PROFILE" \
                --no-first-run \
                --disable-renderer-backgrounding \
                --disable-background-timer-throttling \
                --disable-backgrounding-occluded-windows \
                --app="http://localhost:5001/?fps=$TARGET_FPS" > /dev/null 2>&1 &
            CHROME_PID=$!
            sleep 5  # let Chrome fully initialize all sub-processes
            ;;
    esac

    echo "[sweep] Backend PID=$BACKEND_PID, Chrome PID=${CHROME_PID:-none}"

    # --- Start publisher ---
    echo "[sweep] Starting publisher (${RATE} msg/s, ${DURATION}s)..."
    python3 flood_data.py \
        --rate "$RATE" \
        --duration "$DURATION" \
        --log-dir "$CONFIG_DIR" \
        --trial-id "$CONFIG_ID" &
    PUB_PID=$!

    # --- Sample CPU + memory every second ---
    CPU_LOG="$CONFIG_DIR/cpu_samples.csv"
    echo "elapsed_s,cpu_pct" > "$CPU_LOG"

    echo "[sweep] Sampling resources for ${DURATION}s..."
    sleep 2  # stabilization period

    for i in $(seq 1 "$DURATION"); do
        ALL_PIDS=$(get_all_pids "$BACKEND_PID" "$CHROME_PID")
        CPU_VAL=$(measure_cpu "$ALL_PIDS")
        PSS_VAL=$(measure_pss "$ALL_PIDS")
        RSS_VAL=$(measure_rss "$ALL_PIDS")
        echo "$i,$CPU_VAL" >> "$CPU_LOG"
        echo "$ARCH,$TARGET_FPS,$i,$CPU_VAL,$PSS_VAL,$RSS_VAL" >> "$SUMMARY"
        sleep 1
    done

    # --- Wait for publisher to finish ---
    wait "$PUB_PID" 2>/dev/null || true
    sleep 2

    # --- Stop everything gracefully ---
    if [ -n "$BACKEND_PID" ]; then
        kill -INT "$BACKEND_PID" 2>/dev/null || true
        for _w in $(seq 1 50); do
            kill -0 "$BACKEND_PID" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$BACKEND_PID" 2>/dev/null; then
            echo "  ⚠ Backend did not exit gracefully, force-killing..."
            kill -9 "$BACKEND_PID" 2>/dev/null || true
        fi
        wait "$BACKEND_PID" 2>/dev/null || true
    fi

    if [ -n "$CHROME_PID" ]; then
        # Kill Chrome and all its children
        pkill -P "$CHROME_PID" 2>/dev/null || true
        kill "$CHROME_PID" 2>/dev/null || true
        wait "$CHROME_PID" 2>/dev/null || true
        # Clean up by profile dir (catches any stragglers)
        if [ -n "$CHROME_PROFILE" ]; then
            pkill -f "$CHROME_PROFILE" 2>/dev/null || true
            sleep 1
            rm -rf "$CHROME_PROFILE"
        fi
    fi

    # Clean up any orphans
    pkill -f "dashboard/build/dashboard" 2>/dev/null || true
    pkill -f "node server.js" 2>/dev/null || true

    sleep 3  # cool-down between configs
    echo "  ✓ $CONFIG_ID complete"
    echo ""
  done
done

# ---------------------------------------------------------------------------
# Generate final aggregated summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  FPS Sweep Complete — Aggregated Results"
echo "============================================================"
echo ""

printf "%-20s  %10s  %10s  %10s\n" "Configuration" "Avg CPU%" "Avg PSS MB" "Avg RSS MB"
printf "%-20s  %10s  %10s  %10s\n" "--------------------" "----------" "----------" "----------"

for ARCH in "${ARCHITECTURES[@]}"; do
  for TARGET_FPS in "${FPS_VALUES[@]}"; do
    RESULT=$(awk -F',' -v arch="$ARCH" -v fps="$TARGET_FPS" '
        $1 == arch && $2 == fps {
            cpu_sum += $4; pss_sum += $5; rss_sum += $6; n++
        }
        END {
            if (n > 0)
                printf "%.1f %.1f %.1f", cpu_sum/n, pss_sum/n/1024, rss_sum/n/1024
            else
                printf "N/A N/A N/A"
        }
    ' "$SUMMARY")

    AVG_CPU=$(echo "$RESULT" | awk '{print $1}')
    AVG_PSS=$(echo "$RESULT" | awk '{print $2}')
    AVG_RSS=$(echo "$RESULT" | awk '{print $3}')

    printf "%-20s  %9s%%  %8s MB  %8s MB\n" \
        "${ARCH}_${TARGET_FPS}fps" "$AVG_CPU" "$AVG_PSS" "$AVG_RSS"
  done
done

echo ""
echo "Raw data:  $SUMMARY"
echo "Per-config logs: $BASE_DIR/<config>/cpu_samples.csv"
echo ""
echo "============================================================"
