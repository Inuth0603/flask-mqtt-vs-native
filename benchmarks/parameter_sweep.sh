#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# parameter_sweep.sh — Automated multi-configuration benchmark  (Concern 7)
#
# Runs the publisher + a specified backend across a grid of workload
# parameters (rate, payload size, QoS, duration) to evaluate sensitivity.
#
# Usage:
#   ./parameter_sweep.sh <mode> [trials_per_config]
#
#   mode:   native | node | flask
#   trials_per_config: number of repeated trials per config (default: 5)
#
# The script uses the revised flood_data.py publisher which accepts
# --rate, --payload-size, --qos, --duration CLI arguments.
#
# Results are written to benchmark_logs/sweep_<mode>/
# ---------------------------------------------------------------------------
set -euo pipefail

MODE="${1:?Usage: $0 <native|node|flask> [trials_per_config]}"
N_TRIALS="${2:-5}"

# Parameter grid (Concern 7: evaluate across different configurations)
RATES=(1000 2500 5000 10000 20000)
PAYLOADS=(50 1024)
QOS_LEVELS=(0)
DURATION=60

SWEEP_DIR="benchmark_logs/sweep_${MODE}"
mkdir -p "$SWEEP_DIR"

echo "============================================"
echo " Parameter Sweep: $MODE"
echo " Rates:    ${RATES[*]}"
echo " Payloads: ${PAYLOADS[*]}"
echo " QoS:      ${QOS_LEVELS[*]}"
echo " Duration: ${DURATION}s"
echo " Trials:   $N_TRIALS per config"
echo "============================================"
echo ""

# Write sweep index header
INDEX_FILE="$SWEEP_DIR/sweep_index.csv"
echo "trial_id,rate,payload,qos,duration,messages_published,messages_received,latency_median_us,latency_p95_us,latency_p99_us" \
    > "$INDEX_FILE"

start_backend() {
    case "$MODE" in
        native)
            echo "[sweep] Starting native dashboard..."
            BENCHMARK_LOG_DIR="$TRIAL_DIR" ./dashboard/build/dashboard &
            BACKEND_PID=$!
            ;;
        node)
            echo "[sweep] Starting Node.js backend..."
            cd node_backend
            BENCHMARK_LOG_DIR="../$TRIAL_DIR" node server.js &
            BACKEND_PID=$!
            cd ..
            ;;
        flask)
            echo "[sweep] Starting Flask backend..."
            cd example
            BENCHMARK_LOG_DIR="../$TRIAL_DIR" python app.py &
            BACKEND_PID=$!
            cd ..
            ;;
    esac
    sleep 3  # Let backend stabilize
}

stop_backend() {
    if [ -n "${BACKEND_PID:-}" ]; then
        killall -9 dashboard 2>/dev/null || true
        kill -INT "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
}

for RATE in "${RATES[@]}"; do
  for PAYLOAD in "${PAYLOADS[@]}"; do
    for QOS in "${QOS_LEVELS[@]}"; do
      for TRIAL in $(seq 1 "$N_TRIALS"); do
          TRIAL_ID="${MODE}_r${RATE}_p${PAYLOAD}_q${QOS}_t${TRIAL}"
          TRIAL_DIR="$SWEEP_DIR/$TRIAL_ID"
          mkdir -p "$TRIAL_DIR"

          echo "--------------------------------------------"
          echo " Config: rate=${RATE} payload=${PAYLOAD} qos=${QOS} trial=${TRIAL}"
          echo " Trial ID: $TRIAL_ID"
          echo "--------------------------------------------"

          # Start backend
          start_backend

          # Start CPU & memory measurement in background
          bash benchmarks/measure_cpu.sh "$MODE" "$DURATION" &
          CPU_PID=$!
          bash benchmarks/measure_memory.sh "$MODE" "$DURATION" &
          MEM_PID=$!

          # Run publisher
          python flood_data.py \
              --rate "$RATE" \
              --payload-size "$PAYLOAD" \
              --qos "$QOS" \
              --duration "$DURATION" \
              --log-dir "$TRIAL_DIR" \
              --trial-id "$TRIAL_ID"

          # Wait for measurement scripts
          wait "$CPU_PID" 2>/dev/null || true
          wait "$MEM_PID" 2>/dev/null || true

          # Stop backend (triggers stats dump)
          stop_backend

          sleep 2

          # Collect results into sweep index
          PUB_LOG=$(find "$TRIAL_DIR" -name "publisher_*.log" | head -1)
          MSGS_PUB=$(grep "messages_published" "$PUB_LOG" 2>/dev/null | cut -d= -f2 || echo "0")

          # Find backend summary (native/node/flask)
          SUMMARY=$(find "$TRIAL_DIR" -name "*_summary.log" | head -1)
          MSGS_RCV=$(grep "messages_received" "$SUMMARY" 2>/dev/null | cut -d= -f2 || echo "0")
          LAT_MED=$(grep "latency_median_us" "$SUMMARY" 2>/dev/null | cut -d= -f2 || echo "0")
          LAT_P95=$(grep "latency_p95_us" "$SUMMARY" 2>/dev/null | cut -d= -f2 || echo "0")
          LAT_P99=$(grep "latency_p99_us" "$SUMMARY" 2>/dev/null | cut -d= -f2 || echo "0")

          echo "$TRIAL_ID,$RATE,$PAYLOAD,$QOS,$DURATION,$MSGS_PUB,$MSGS_RCV,$LAT_MED,$LAT_P95,$LAT_P99" \
              >> "$INDEX_FILE"

          echo "[sweep] Trial $TRIAL_ID complete."
          echo ""
      done
    done
  done
done

echo "============================================"
echo " Sweep complete. Index: $INDEX_FILE"
echo "============================================"
