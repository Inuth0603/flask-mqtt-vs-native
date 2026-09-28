#!/usr/bin/env python3
"""
flask_async_variants.py — Flask async configuration comparison  (Concern 6)

Tests four Flask-SocketIO async configurations to determine whether the
observed memory bloat is a fundamental ecosystem issue or a configuration-
specific pitfall:

  1. eventlet WITH monkey_patch()        → expected: stable
  2. eventlet WITHOUT monkey_patch()     → expected: bloat (current paper config)
  3. gevent WITH monkey_patch()          → expected: stable
  4. threading mode (no async lib)       → expected: stable but slower

Each variant runs under a 5-minute sustained 5,000 msg/sec load.
Memory (RSS) is sampled at 10-second intervals.

Usage:
    python benchmarks/flask_async_variants.py [--rate 5000] [--duration 300]

Prerequisites:
    pip install eventlet gevent flask flask-socketio flask-mqtt flask-bootstrap

Output:
    benchmark_logs/async_variants/variant_<name>_memory.csv
    benchmark_logs/async_variants/summary.csv
"""
import argparse
import csv
import os
import signal
import subprocess
import sys
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Flask async variant benchmark")
    parser.add_argument("--rate", type=int, default=5000, help="Publish rate (default: 5000)")
    parser.add_argument("--duration", type=int, default=300, help="Duration in seconds (default: 300)")
    parser.add_argument("--sample-interval", type=int, default=10, help="Memory sample interval (default: 10s)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Flask app variants — each is a small self-contained script written to a
# temporary file and executed as a subprocess.
# ---------------------------------------------------------------------------

VARIANT_EVENTLET_PATCHED = """\
import eventlet
eventlet.monkey_patch()  # <-- the critical call

from flask import Flask
from flask_mqtt import Mqtt
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET'] = 'bench'
app.config['MQTT_BROKER_URL'] = 'localhost'
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_CLIENT_ID'] = 'flask_variant_eventlet_patched'
app.config['MQTT_CLEAN_SESSION'] = True
app.config['MQTT_USERNAME'] = ''
app.config['MQTT_PASSWORD'] = ''
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False

mqtt_client = Mqtt()
socketio = SocketIO(app, async_mode='eventlet')

msg_count = 0

@mqtt_client.on_message()
def handle_msg(client, userdata, message):
    global msg_count
    msg_count += 1
    socketio.emit('mqtt_message', {'payload': message.payload.decode()})

@mqtt_client.on_connect()
def on_connect(client, userdata, flags, rc):
    mqtt_client.subscribe('test/topic', 0)

mqtt_client.init_app(app)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, use_reloader=False, debug=False)
"""

VARIANT_EVENTLET_UNPATCHED = """\
# NOTE: intentionally NO monkey_patch() — this is the broken config from the paper
from flask import Flask
from flask_mqtt import Mqtt
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET'] = 'bench'
app.config['MQTT_BROKER_URL'] = 'localhost'
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_CLIENT_ID'] = 'flask_variant_eventlet_unpatched'
app.config['MQTT_CLEAN_SESSION'] = True
app.config['MQTT_USERNAME'] = ''
app.config['MQTT_PASSWORD'] = ''
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False

mqtt_client = Mqtt()
socketio = SocketIO(app)   # defaults to eventlet if installed

msg_count = 0

@mqtt_client.on_message()
def handle_msg(client, userdata, message):
    global msg_count
    msg_count += 1
    socketio.emit('mqtt_message', {'payload': message.payload.decode()})

@mqtt_client.on_connect()
def on_connect(client, userdata, flags, rc):
    mqtt_client.subscribe('test/topic', 0)

mqtt_client.init_app(app)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, use_reloader=False, debug=False)
"""

VARIANT_GEVENT_PATCHED = """\
from gevent import monkey
monkey.patch_all()

from flask import Flask
from flask_mqtt import Mqtt
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET'] = 'bench'
app.config['MQTT_BROKER_URL'] = 'localhost'
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_CLIENT_ID'] = 'flask_variant_gevent_patched'
app.config['MQTT_CLEAN_SESSION'] = True
app.config['MQTT_USERNAME'] = ''
app.config['MQTT_PASSWORD'] = ''
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False

mqtt_client = Mqtt()
socketio = SocketIO(app, async_mode='gevent')

msg_count = 0

@mqtt_client.on_message()
def handle_msg(client, userdata, message):
    global msg_count
    msg_count += 1
    socketio.emit('mqtt_message', {'payload': message.payload.decode()})

@mqtt_client.on_connect()
def on_connect(client, userdata, flags, rc):
    mqtt_client.subscribe('test/topic', 0)

mqtt_client.init_app(app)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, use_reloader=False, debug=False)
"""

VARIANT_THREADING = """\
from flask import Flask
from flask_mqtt import Mqtt
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET'] = 'bench'
app.config['MQTT_BROKER_URL'] = 'localhost'
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_CLIENT_ID'] = 'flask_variant_threading'
app.config['MQTT_CLEAN_SESSION'] = True
app.config['MQTT_USERNAME'] = ''
app.config['MQTT_PASSWORD'] = ''
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False

mqtt_client = Mqtt()
socketio = SocketIO(app, async_mode='threading')

msg_count = 0

@mqtt_client.on_message()
def handle_msg(client, userdata, message):
    global msg_count
    msg_count += 1
    socketio.emit('mqtt_message', {'payload': message.payload.decode()})

@mqtt_client.on_connect()
def on_connect(client, userdata, flags, rc):
    mqtt_client.subscribe('test/topic', 0)

mqtt_client.init_app(app)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, use_reloader=False, debug=False)
"""

VARIANTS = {
    "eventlet_patched":   VARIANT_EVENTLET_PATCHED,
    "eventlet_unpatched": VARIANT_EVENTLET_UNPATCHED,
    "gevent_patched":     VARIANT_GEVENT_PATCHED,
    "threading":          VARIANT_THREADING,
}


def get_rss_kb(pid):
    """Read RSS from /proc/<pid>/status (Linux only)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def run_variant(name, script_code, args, out_dir):
    """Run a single Flask variant and sample its memory over time."""
    print(f"\n{'='*60}")
    print(f"  Variant: {name}")
    print(f"  Duration: {args.duration}s, Rate: {args.rate} msg/s")
    print(f"{'='*60}")

    # Write the variant script to a temp file
    script_path = os.path.join(out_dir, f"variant_{name}.py")
    with open(script_path, "w") as f:
        f.write(script_code)

    # Start the Flask variant
    proc = subprocess.Popen(
        [sys.executable, script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(3)  # Let it start up

    # Start the publisher
    pub_proc = subprocess.Popen(
        [
            sys.executable, "flood_data.py",
            "--rate", str(args.rate),
            "--duration", str(args.duration),
            "--log-dir", out_dir,
            "--trial-id", name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Sample memory at intervals
    csv_path = os.path.join(out_dir, f"variant_{name}_memory.csv")
    samples = []
    with open(csv_path, "w", newline="") as csvf:
        writer = csv.writer(csvf)
        writer.writerow(["elapsed_s", "rss_kb"])

        start = time.time()
        while time.time() - start < args.duration + 5:
            elapsed = int(time.time() - start)
            rss = get_rss_kb(proc.pid)
            writer.writerow([elapsed, rss])
            samples.append((elapsed, rss))
            if proc.poll() is not None:
                print(f"  [!] Process exited early at t={elapsed}s")
                break
            time.sleep(args.sample_interval)

    # Stop publisher
    if pub_proc.poll() is None:
        pub_proc.terminate()
        pub_proc.wait(timeout=10)

    # Stop Flask variant
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Summary
    if samples:
        rss_values = [s[1] for s in samples if s[1] > 0]
        if rss_values:
            initial = rss_values[0]
            final = rss_values[-1]
            peak = max(rss_values)
            growth = final - initial
            print(f"  Initial RSS: {initial / 1024:.1f} MB")
            print(f"  Final RSS:   {final / 1024:.1f} MB")
            print(f"  Peak RSS:    {peak / 1024:.1f} MB")
            print(f"  Growth:      {growth / 1024:.1f} MB")
            return {
                "variant": name,
                "initial_mb": initial / 1024,
                "final_mb": final / 1024,
                "peak_mb": peak / 1024,
                "growth_mb": growth / 1024,
                "crashed": proc.returncode != 0 if proc.returncode is not None else False,
            }

    return {"variant": name, "initial_mb": 0, "final_mb": 0, "peak_mb": 0, "growth_mb": 0, "crashed": True}


def main():
    args = parse_args()
    out_dir = os.path.join(
        os.environ.get("BENCHMARK_LOG_DIR", "benchmark_logs"),
        "async_variants"
    )
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for name, code in VARIANTS.items():
        try:
            result = run_variant(name, code, args, out_dir)
            results.append(result)
        except Exception as e:
            print(f"  [ERROR] Variant {name} failed: {e}")
            results.append({"variant": name, "error": str(e)})

    # Write summary
    summary_path = os.path.join(out_dir, "summary.csv")
    with open(summary_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "variant", "initial_mb", "final_mb", "peak_mb", "growth_mb", "crashed"
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in writer.fieldnames})

    print(f"\n{'='*60}")
    print(f"  All variants complete. Summary: {summary_path}")
    print(f"{'='*60}")

    # Print summary table
    print(f"\n{'Variant':<25} {'Initial':>10} {'Final':>10} {'Peak':>10} {'Growth':>10} {'Crashed':>8}")
    print("-" * 75)
    for r in results:
        print(f"{r.get('variant','?'):<25} "
              f"{r.get('initial_mb',0):>9.1f}M "
              f"{r.get('final_mb',0):>9.1f}M "
              f"{r.get('peak_mb',0):>9.1f}M "
              f"{r.get('growth_mb',0):>9.1f}M "
              f"{'YES' if r.get('crashed') else 'no':>8}")


if __name__ == "__main__":
    main()
