"""

Flask-MQTT Telemetry Backend (Revision 2)

Changes from v1:
  - Message counter for end-to-end delivery verification (Concern 1)
  - Publish-to-receive latency measurement using embedded timestamps (Concern 4)
  - Stats dump on shutdown

"""
import atexit
import logging
import json
import os
import time
import signal
import sys
from flask import Flask, render_template
from flask_mqtt import Mqtt
from flask_socketio import SocketIO
from flask_bootstrap import Bootstrap
# if SSL enabled
# from flask_mqtt import ssl

app = Flask(__name__)
app.config['SECRET'] = 'my secret key'
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
app.config['MQTT_BROKER_URL'] = 'localhost'
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_CLIENT_ID'] = 'flask_mqtt'
app.config['MQTT_CLEAN_SESSION'] = True
app.config['MQTT_USERNAME'] = ''
app.config['MQTT_PASSWORD'] = ''
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False
app.config['MQTT_LAST_WILL_TOPIC'] = 'home/lastwill'
app.config['MQTT_LAST_WILL_MESSAGE'] = 'bye'
app.config['MQTT_LAST_WILL_QOS'] = 0

# Parameters for SSL enabled
# app.config['MQTT_BROKER_PORT'] = 8883
# app.config['MQTT_TLS_ENABLED'] = True
# app.config['MQTT_TLS_INSECURE'] = True
# app.config['MQTT_TLS_CA_CERTS'] = 'ca.crt'

# Parmeters for SSL with client certificate
# app.config['MQTT_TLS_ENABLED']   = True
# app.config['MQTT_BROKER_URL']    = 'test.mosquitto.org'
# app.config['MQTT_BROKER_PORT']   = 8884
# app.config['MQTT_TLS_INSECURE']  = False
# app.config['MQTT_TLS_CA_CERTS']  = 'mosquitto.org.crt'
# app.config['MQTT_TLS_CERTFILE']  = 'client.crt'
# app.config['MQTT_TLS_KEYFILE']   = 'client.key'
# app.config['MQTT_TLS_VERSION']   = ssl.PROTOCOL_TLSv1_2
# app.config['MQTT_TLS_CERT_REQS'] = True


mqtt = Mqtt()
socketio = SocketIO(app)
bootstrap = Bootstrap(app)

# ---------------------------------------------------------------------------
# Instrumentation: message counter & latency  (Concerns 1 & 4)
# ---------------------------------------------------------------------------
msg_count = 0
latency_samples_us = []   # microseconds


def compute_latency_us(payload_str):
    """Parse the embedded monotonic-ns timestamp and return latency in µs."""
    sep = payload_str.find('|')
    if sep == -1:
        return -1.0
    try:
        pub_ns = int(payload_str[:sep])
    except ValueError:
        return -1.0
    now_ns = time.monotonic_ns()
    if now_ns < pub_ns:
        return 0.0
    return (now_ns - pub_ns) / 1000.0   # → microseconds


def dump_stats():
    """Write instrumentation data to stdout and log files."""
    print(f"\n=== Flask Backend Stats ===")
    print(f"Total messages received: {msg_count}")

    if latency_samples_us:
        samples = sorted(latency_samples_us)
        n = len(samples)
        median = samples[n // 2]
        p95 = samples[int(n * 0.95)]
        p99 = samples[int(n * 0.99)]
        avg = sum(samples) / n

        print(f"Latency samples:         {n}")
        print(f"  Mean:   {avg / 1000:.3f} ms")
        print(f"  Median: {median / 1000:.3f} ms")
        print(f"  p95:    {p95 / 1000:.3f} ms")
        print(f"  p99:    {p99 / 1000:.3f} ms")

        log_dir = os.environ.get("BENCHMARK_LOG_DIR", "benchmark_logs")
        os.makedirs(log_dir, exist_ok=True)

        csv_path = os.path.join(log_dir, "flask_latency.csv")
        with open(csv_path, "w") as f:
            f.write("latency_us\n")
            for v in samples:
                f.write(f"{v:.2f}\n")
        print(f"Latency log written to {csv_path}")

        summary_path = os.path.join(log_dir, "flask_summary.log")
        with open(summary_path, "w") as f:
            f.write(f"messages_received={msg_count}\n")
            f.write(f"routing_latency_median_us={median:.2f}\n")
            f.write(f"routing_latency_p95_us={p95:.2f}\n")
            f.write(f"routing_latency_p99_us={p99:.2f}\n")
            # Include browser-reported display latency if available
            if browser_stats:
                for key in ["display_latency_median_us", "display_latency_p95_us",
                             "display_latency_p99_us", "frontend_messages_received",
                             "last_fps"]:
                    if key in browser_stats:
                        f.write(f"{key}={browser_stats[key]}\n")
                if "frame_time_mean_ms" in browser_stats:
                    f.write(f"frame_time_mean_ms={browser_stats['frame_time_mean_ms']:.3f}\n")
                    f.write(f"frame_time_max_ms={browser_stats['frame_time_max_ms']:.3f}\n")
                    f.write(f"dropped_frames={browser_stats['dropped_frames']}\n")


atexit.register(dump_stats)

# Handle SIGINT gracefully so atexit/dump_stats actually runs
def _sigint_handler(sig, frame):
    dump_stats()
    sys.exit(0)

signal.signal(signal.SIGINT, _sigint_handler)
signal.signal(signal.SIGTERM, _sigint_handler)


# ---------------------------------------------------------------------------
# Browser-side benchmark stats receiver  (Concerns 4 & 5)
# ---------------------------------------------------------------------------
browser_stats = None

@socketio.on('benchmark_stats')
def handle_benchmark_stats(json_str):
    global browser_stats
    try:
        stats = json.loads(json_str)
        browser_stats = stats

        log_dir = os.environ.get("BENCHMARK_LOG_DIR", "benchmark_logs")
        os.makedirs(log_dir, exist_ok=True)

        stats_path = os.path.join(log_dir, "browser_stats.json")
        with open(stats_path, "w") as f:
            json.dump(stats, f, indent=2)
        print(f"[benchmark] Browser stats written to {stats_path}")
    except Exception as e:
        print(f"[benchmark] Error handling browser stats: {e}")



@app.route('/')
def index():
    return render_template('index.html')

@app.route('/canvas')
def canvas():
    return render_template('canvas.html')


@socketio.on('publish')
def handle_publish(json_str):
    data = json.loads(json_str)
    mqtt.publish(data['topic'], data['message'], data['qos'])


@socketio.on('subscribe')
def handle_subscribe(json_str):
    # Subscription is already handled on connect
    pass


@socketio.on('unsubscribe_all')
def handle_unsubscribe_all():
    mqtt.unsubscribe_all()


@mqtt.on_message()
def handle_mqtt_message(client, userdata, message):
    global msg_count

    payload_str = message.payload.decode()

    # --- Concern 1: count every received message ---
    msg_count += 1

    # --- Concern 4: latency measurement ---
    lat = compute_latency_us(payload_str)
    if lat >= 0:
        latency_samples_us.append(lat)
        
        now_wall_ms = time.time() * 1000
        sep = payload_str.find('|')
        if sep != -1:
            payload_str = f"{now_wall_ms}|{lat:.2f}|{payload_str[sep+1:]}"

    data = dict(
        topic=message.topic,
        payload=payload_str,
        qos=message.qos,
    )
    socketio.emit('mqtt_message', data=data)


@mqtt.on_log()
def handle_logging(client, userdata, level, buf):
    # print(level, buf)
    pass

@mqtt.on_connect()
def handle_connect(client, userdata, flags, rc):
    mqtt.subscribe('test/topic', 0)

# Initialize MQTT after registering event handlers
mqtt.init_app(app)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, use_reloader=False, debug=False)
