#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_trials.sh — Proper interleaved trial runner
#
# Fixes from audit:
#   - Interleaves architectures to avoid thermal/drift bias
#   - Uses fresh Chrome profile per trial with throttling flags disabled
#   - Logs broker $SYS counters before/after each trial
#   - Captures system environment info
#   - Includes naive DOM variant
#
# Usage:
#   ./benchmarks/run_trials.sh [num_trials] [duration_seconds]
#
#   num_trials: trials PER architecture (default: 10)
#   duration:   seconds per trial (default: 60)
# ---------------------------------------------------------------------------
set -euo pipefail

N_TRIALS="${1:-10}"
DURATION="${2:-60}"
RATE=5000
BASE_LOG_DIR="benchmark_logs/formal_trials"

ARCHITECTURES=("native" "node" "flask_canvas" "flask_dom")

mkdir -p "$BASE_LOG_DIR"

# ---------------------------------------------------------------------------
# Log system environment (once)
# ---------------------------------------------------------------------------
ENV_FILE="$BASE_LOG_DIR/environment.txt"
echo "=== System Environment ===" > "$ENV_FILE"
echo "Date: $(date -Iseconds)" >> "$ENV_FILE"
uname -a >> "$ENV_FILE" 2>/dev/null || true
echo "---" >> "$ENV_FILE"
head -20 /proc/cpuinfo >> "$ENV_FILE" 2>/dev/null || true
echo "---" >> "$ENV_FILE"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor >> "$ENV_FILE" 2>/dev/null || echo "governor: unknown" >> "$ENV_FILE"
echo "" >> "$ENV_FILE"
cat /sys/devices/system/cpu/intel_pstate/no_turbo >> "$ENV_FILE" 2>/dev/null || echo "turbo: unknown" >> "$ENV_FILE"
echo "---" >> "$ENV_FILE"
echo "Display: ${XDG_SESSION_TYPE:-unknown}" >> "$ENV_FILE"
google-chrome --version >> "$ENV_FILE" 2>/dev/null || chromium-browser --version >> "$ENV_FILE" 2>/dev/null || echo "chrome: not found" >> "$ENV_FILE"
mosquitto -h 2>&1 | head -1 >> "$ENV_FILE" || true
python3 --version >> "$ENV_FILE" 2>/dev/null || true
node --version >> "$ENV_FILE" 2>/dev/null || true
echo "---" >> "$ENV_FILE"
free -h >> "$ENV_FILE" 2>/dev/null || true
echo "" >> "$ENV_FILE"
cat "$ENV_FILE"

# ---------------------------------------------------------------------------
# Helper: get broker message counters via $SYS
# ---------------------------------------------------------------------------
get_broker_stats() {
    local label="$1"
    local out_file="$2"
    # Read $SYS topics (timeout after 1 second)
    local received sent
    received=$(mosquitto_sub -t '$SYS/broker/messages/received' -C 1 -W 2 2>/dev/null || echo "0")
    sent=$(mosquitto_sub -t '$SYS/broker/messages/sent' -C 1 -W 2 2>/dev/null || echo "0")
    echo "${label}_broker_received=$received" >> "$out_file"
    echo "${label}_broker_sent=$sent" >> "$out_file"
}

# ---------------------------------------------------------------------------
# Helper: launch Chrome with a fresh profile
# ---------------------------------------------------------------------------
CHROME_CMD=""
if command -v google-chrome &>/dev/null; then
    CHROME_CMD="google-chrome"
elif command -v chromium-browser &>/dev/null; then
    CHROME_CMD="chromium-browser"
elif command -v chromium &>/dev/null; then
    CHROME_CMD="chromium"
fi

launch_chrome() {
    local url="$1"
    local profile_dir
    profile_dir=$(mktemp -d)
    echo "$profile_dir"  # caller captures this

    $CHROME_CMD \
        --user-data-dir="$profile_dir" \
        --no-first-run \
        --disable-renderer-backgrounding \
        --disable-background-timer-throttling \
        --disable-backgrounding-occluded-windows \
        --app="$url" &
    CHROME_PID=$!
    echo "$CHROME_PID" > "$profile_dir/chrome.pid"
}

kill_chrome() {
    local profile_dir="$1"
    local pid
    pid=$(cat "$profile_dir/chrome.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ]; then
        # Kill the entire process group
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    # Also kill any stray chromium for this profile
    pkill -f "$profile_dir" 2>/dev/null || true
    sleep 1
    rm -rf "$profile_dir"
}

# ---------------------------------------------------------------------------
# Build interleaved trial order
# ---------------------------------------------------------------------------
TRIAL_ORDER=()
for i in $(seq 1 "$N_TRIALS"); do
    # Shuffle architectures for each round
    SHUFFLED=($(shuf -e "${ARCHITECTURES[@]}"))
    for arch in "${SHUFFLED[@]}"; do
        TRIAL_ORDER+=("${arch}:${i}")
    done
done

echo ""
echo "============================================"
echo " Formal N=$N_TRIALS Trials (Interleaved)"
echo " Architectures: ${ARCHITECTURES[*]}"
echo " Duration: ${DURATION}s at ${RATE} msg/s"
echo " Total runs: ${#TRIAL_ORDER[@]}"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Run each trial
# ---------------------------------------------------------------------------
for entry in "${TRIAL_ORDER[@]}"; do
    ARCH="${entry%%:*}"
    TRIAL_NUM="${entry##*:}"
    TRIAL_ID="${ARCH}_trial_${TRIAL_NUM}"
    TRIAL_DIR="$BASE_LOG_DIR/$TRIAL_ID"
    mkdir -p "$TRIAL_DIR"

    echo "--------------------------------------------"
    echo " $TRIAL_ID  ($(date +%H:%M:%S))"
    echo "--------------------------------------------"

    # Broker stats BEFORE
    get_broker_stats "before" "$TRIAL_DIR/broker_stats.log"

    export BENCHMARK_LOG_DIR="$TRIAL_DIR"
    BACKEND_PID=""
    CHROME_PROFILE=""

    case "$ARCH" in
        native)
            ./dashboard/build/dashboard &
            BACKEND_PID=$!
            sleep 3
            ;;
        node)
            cd node_backend
            BENCHMARK_LOG_DIR="../$TRIAL_DIR" node server.js &
            BACKEND_PID=$!
            cd ..
            sleep 3
            CHROME_PROFILE=$(launch_chrome "http://localhost:5001")
            sleep 3
            ;;
        flask_canvas)
            cd example
            BENCHMARK_LOG_DIR="../$TRIAL_DIR" python3 app.py &
            BACKEND_PID=$!
            cd ..
            sleep 3
            CHROME_PROFILE=$(launch_chrome "http://localhost:5000/canvas")
            sleep 3
            ;;
        flask_dom)
            cd example
            BENCHMARK_LOG_DIR="../$TRIAL_DIR" python3 app.py &
            BACKEND_PID=$!
            cd ..
            sleep 3
            CHROME_PROFILE=$(launch_chrome "http://localhost:5000/")
            sleep 3
            ;;
    esac

    # Start CPU & memory measurement
    BENCHMARK_LOG_DIR="$TRIAL_DIR" bash benchmarks/measure_cpu.sh "$ARCH" "$DURATION" &
    CPU_PID=$!
    BENCHMARK_LOG_DIR="$TRIAL_DIR" bash benchmarks/measure_memory.sh "$ARCH" "$DURATION" &
    MEM_PID=$!

    # Run publisher
    python3 flood_data.py \
        --rate "$RATE" \
        --duration "$DURATION" \
        --log-dir "$TRIAL_DIR" \
        --trial-id "$TRIAL_ID"

    # Wait for measurement scripts
    wait "$CPU_PID" 2>/dev/null || true
    wait "$MEM_PID" 2>/dev/null || true
    sleep 2

    # Broker stats AFTER
    get_broker_stats "after" "$TRIAL_DIR/broker_stats.log"

    # Stop everything
    if [ -n "$BACKEND_PID" ]; then
        killall -9 dashboard 2>/dev/null || true
        kill -INT "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "$CHROME_PROFILE" ]; then
        kill_chrome "$CHROME_PROFILE"
    fi

    sleep 3  # cool-down between trials
    echo " ✓ $TRIAL_ID complete"
    echo ""
done

echo "============================================"
echo " All trials complete!"
echo " Results: $BASE_LOG_DIR/"
echo " Run: python3 benchmarks/aggregate_results.py --log-dir $BASE_LOG_DIR"
echo "============================================"
