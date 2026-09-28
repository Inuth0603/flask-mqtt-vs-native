"""
MQTT Telemetry Flood Publisher (Revision 2)

Changes from v1:
  - Embeds a monotonic nanosecond timestamp in every payload so subscribers
    can compute end-to-end latency on the same host.
  - Accepts CLI arguments for --rate, --payload-size, --qos, --duration
    to support the parameter-sweep experiments.
  - Writes a per-trial summary (published count, achieved rate) to a log file.
"""
import argparse
import os
import time
import sys

import paho.mqtt.client as mqtt

BROKER_ADDRESS = "localhost"
TOPIC = "test/topic"


def parse_args():
    parser = argparse.ArgumentParser(
        description="High-frequency MQTT publisher for telemetry benchmarks."
    )
    parser.add_argument(
        "--rate", type=int, default=5000,
        help="Target publish rate in messages/sec (default: 5000)"
    )
    parser.add_argument(
        "--payload-size", type=int, default=0,
        help="Minimum payload size in bytes. 0 = natural size only (default: 0)"
    )
    parser.add_argument(
        "--qos", type=int, choices=[0, 1, 2], default=0,
        help="MQTT QoS level (default: 0)"
    )
    parser.add_argument(
        "--duration", type=int, default=60,
        help="Duration of the flood in seconds (default: 60)"
    )
    parser.add_argument(
        "--log-dir", type=str, default="benchmark_logs",
        help="Directory for per-trial summary logs (default: benchmark_logs)"
    )
    parser.add_argument(
        "--trial-id", type=str, default=None,
        help="Optional trial identifier for the log filename"
    )
    return parser.parse_args()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to broker!")
    else:
        print(f"Failed to connect, return code {rc}")


def build_payload(packet_num, min_size):
    """Build a payload with an embedded monotonic timestamp.

    Format: ``<monotonic_ns>|Data Packet #<n>``
    Optionally padded to *min_size* bytes.
    """
    ts_ns = time.monotonic_ns()
    body = f"{ts_ns}|Data Packet #{packet_num}"
    if min_size and len(body) < min_size:
        body += "X" * (min_size - len(body))
    return body


def main():
    args = parse_args()
    interval = 1.0 / args.rate

    client = mqtt.Client()
    client.on_connect = on_connect

    print(f"Connecting to {BROKER_ADDRESS}...")
    try:
        client.connect(BROKER_ADDRESS, 1883, 60)
    except Exception as e:
        print(f"Error connecting: {e}")
        sys.exit(1)

    client.loop_start()

    print(
        f"Starting data flood: rate={args.rate} msg/s, qos={args.qos}, "
        f"min_payload={args.payload_size}B, duration={args.duration}s"
    )

    packet_num = 0
    start_time = time.perf_counter()
    next_time = time.perf_counter()
    end_time = start_time + args.duration

    try:
        while time.perf_counter() < end_time:
            packet_num += 1
            payload = build_payload(packet_num, args.payload_size)
            client.publish(TOPIC, payload, qos=args.qos)

            next_time += interval
            sleep_time = next_time - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        print("\nStopping data flood (interrupted).")
    finally:
        elapsed = time.perf_counter() - start_time
        actual_rate = packet_num / elapsed if elapsed > 0 else 0

        print(f"--- Publisher Results ---")
        print(f"Total messages published: {packet_num}")
        print(f"Total elapsed time:       {elapsed:.2f} s")
        print(f"Achieved rate:            {actual_rate:.2f} msg/s")

        # Write summary log
        os.makedirs(args.log_dir, exist_ok=True)
        trial_tag = args.trial_id or time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(
            args.log_dir,
            f"publisher_{trial_tag}_r{args.rate}_q{args.qos}_p{args.payload_size}.log"
        )
        with open(log_path, "w") as f:
            f.write(f"trial_id={trial_tag}\n")
            f.write(f"target_rate={args.rate}\n")
            f.write(f"qos={args.qos}\n")
            f.write(f"payload_size={args.payload_size}\n")
            f.write(f"duration_target={args.duration}\n")
            f.write(f"messages_published={packet_num}\n")
            f.write(f"elapsed_seconds={elapsed:.4f}\n")
            f.write(f"achieved_rate={actual_rate:.2f}\n")
        print(f"Summary written to {log_path}")

        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
