#!/usr/bin/env python3
"""
aggregate_results.py — Statistical aggregation across all trials

Reads all trial directories produced by run_trials.sh and computes:
  - Mean ± SD for all metrics across N trials per architecture
  - Welch's t-test for key comparisons
  - Message delivery verification (publisher vs receiver counts)

Usage:
    python benchmarks/aggregate_results.py --log-dir benchmark_logs/formal_trials

Output:
    Printed summary tables + benchmark_logs/formal_trials/aggregated_results.csv
"""
import argparse
import csv
import math
import os
import re
import statistics
import sys
from collections import defaultdict


def parse_args():
    parser = argparse.ArgumentParser(description="Aggregate benchmark results")
    parser.add_argument("--log-dir", required=True, help="Directory with trial subdirectories")
    return parser.parse_args()


def read_log_value(filepath, key):
    """Read a key=value from a log file, return as float or None."""
    if not os.path.exists(filepath):
        return None
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                try:
                    return float(line.split("=", 1)[1])
                except ValueError:
                    return None
    return None


def read_csv_avg(filepath, column):
    """Read a CSV and return the average of a column."""
    if not os.path.exists(filepath):
        return None
    values = []
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                values.append(float(row[column]))
            except (ValueError, KeyError):
                continue
    return statistics.mean(values) if values else None


def welch_t_test(a, b):
    """Welch's t-test for two independent samples. Returns (t, df, p_approx)."""
    if len(a) < 2 or len(b) < 2:
        return None, None, None

    n1, n2 = len(a), len(b)
    m1, m2 = statistics.mean(a), statistics.mean(b)
    v1, v2 = statistics.variance(a), statistics.variance(b)

    if v1 == 0 and v2 == 0:
        return float('inf'), n1 + n2 - 2, 0.0

    se = math.sqrt(v1/n1 + v2/n2)
    if se == 0:
        return float('inf'), n1 + n2 - 2, 0.0

    t = (m1 - m2) / se

    # Welch-Satterthwaite degrees of freedom
    num = (v1/n1 + v2/n2) ** 2
    den = (v1/n1)**2 / (n1-1) + (v2/n2)**2 / (n2-1)
    df = num / den if den > 0 else n1 + n2 - 2

    # Rough p-value approximation using t-distribution tail
    # For a proper p-value, use scipy.stats.t.sf — this is a fallback
    p = None
    try:
        from scipy.stats import t as t_dist
        p = 2 * t_dist.sf(abs(t), df)
    except ImportError:
        # Very rough approximation
        if abs(t) > 4.0:
            p = 0.001
        elif abs(t) > 2.5:
            p = 0.02
        elif abs(t) > 2.0:
            p = 0.05
        else:
            p = 0.1

    return t, df, p


def main():
    args = parse_args()
    log_dir = args.log_dir

    if not os.path.isdir(log_dir):
        print(f"Error: {log_dir} not found")
        sys.exit(1)

    # Discover trial directories
    arch_data = defaultdict(lambda: defaultdict(list))

    for entry in sorted(os.listdir(log_dir)):
        trial_dir = os.path.join(log_dir, entry)
        if not os.path.isdir(trial_dir):
            continue

        # Parse architecture from directory name: <arch>_trial_<n>
        match = re.match(r"^(.+)_trial_(\d+)$", entry)
        if not match:
            continue

        arch = match.group(1)
        trial_num = int(match.group(2))

        # Messages published
        pub_files = [f for f in os.listdir(trial_dir) if f.startswith("publisher_")]
        for pf in pub_files:
            v = read_log_value(os.path.join(trial_dir, pf), "messages_published")
            if v is not None:
                arch_data[arch]["messages_published"].append(v)

        # Messages received
        summary_files = [f for f in os.listdir(trial_dir) if f.endswith("_summary.log")]
        for sf in summary_files:
            v = read_log_value(os.path.join(trial_dir, sf), "messages_received")
            if v is not None:
                arch_data[arch]["messages_received"].append(v)

            # Display latency
            for key in ["display_latency_median_us", "display_latency_p95_us", "display_latency_p99_us"]:
                v = read_log_value(os.path.join(trial_dir, sf), key)
                if v is not None:
                    arch_data[arch][key].append(v)

            # FPS
            v = read_log_value(os.path.join(trial_dir, sf), "measured_fps")
            if v is not None:
                arch_data[arch]["measured_fps"].append(v)

            # Dropped frames
            v = read_log_value(os.path.join(trial_dir, sf), "dropped_frames")
            if v is not None:
                arch_data[arch]["dropped_frames"].append(v)

        # CPU (average from CSV)
        cpu_files = [f for f in os.listdir(trial_dir) if f.endswith("_cpu.csv")]
        for cf in cpu_files:
            v = read_csv_avg(os.path.join(trial_dir, cf), "cpu_total_pct")
            if v is not None:
                arch_data[arch]["total_cpu_pct"].append(v)

        # Memory — PSS (average from CSV)
        mem_files = [f for f in os.listdir(trial_dir) if f.endswith("_memory.csv")]
        for mf in mem_files:
            for col in ["rss_kb", "pss_kb", "uss_kb"]:
                v = read_csv_avg(os.path.join(trial_dir, mf), col)
                if v is not None:
                    arch_data[arch][col].append(v)

        # Broker stats
        broker_file = os.path.join(trial_dir, "broker_stats.log")
        if os.path.exists(broker_file):
            before_recv = read_log_value(broker_file, "before_broker_received")
            after_recv = read_log_value(broker_file, "after_broker_received")
            if before_recv is not None and after_recv is not None:
                arch_data[arch]["broker_messages"].append(after_recv - before_recv)

        # Browser-side stats JSON (Concerns 4 & 5)
        browser_json = os.path.join(trial_dir, "browser_stats.json")
        if os.path.exists(browser_json):
            try:
                import json
                with open(browser_json) as f:
                    bstats = json.load(f)
                for key in ["display_latency_median_us", "display_latency_p95_us",
                            "display_latency_p99_us"]:
                    if key in bstats and bstats[key]:
                        arch_data[arch][f"browser_{key}"].append(bstats[key])
                if "frontend_messages_received" in bstats:
                    arch_data[arch]["frontend_messages_received"].append(
                        bstats["frontend_messages_received"])
                if "frame_time_mean_ms" in bstats:
                    arch_data[arch]["browser_frame_time_mean_ms"].append(
                        bstats["frame_time_mean_ms"])
                if "dropped_frames" in bstats:
                    arch_data[arch]["browser_dropped_frames"].append(
                        bstats["dropped_frames"])
                if "last_fps" in bstats:
                    arch_data[arch]["browser_fps"].append(bstats["last_fps"])
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Print results
    # -----------------------------------------------------------------------
    def fmt_mean_sd(values, scale=1.0, unit=""):
        if not values:
            return "—"
        m = statistics.mean(values) / scale
        s = statistics.stdev(values) / scale if len(values) > 1 else 0
        return f"{m:.1f} ± {s:.1f}{unit}"

    print("\n" + "=" * 90)
    print("  AGGREGATED RESULTS")
    print("=" * 90)

    metrics = [
        ("messages_published", "Messages Published", 1, ""),
        ("messages_received", "Messages Received (backend)", 1, ""),
        ("frontend_messages_received", "Messages Received (browser)", 1, ""),
        ("broker_messages", "Broker Messages (delta)", 1, ""),
        ("total_cpu_pct", "Total CPU% (full stack)", 1, "%"),
        ("rss_kb", "Total RSS", 1024, " MB"),
        ("pss_kb", "Total PSS", 1024, " MB"),
        ("uss_kb", "Total USS", 1024, " MB"),
        ("display_latency_median_us", "Display Latency Median (backend)", 1000, " ms"),
        ("display_latency_p95_us", "Display Latency p95 (backend)", 1000, " ms"),
        ("display_latency_p99_us", "Display Latency p99 (backend)", 1000, " ms"),
        ("browser_display_latency_median_us", "Display Latency Median (browser)", 1000, " ms"),
        ("browser_display_latency_p95_us", "Display Latency p95 (browser)", 1000, " ms"),
        ("browser_display_latency_p99_us", "Display Latency p99 (browser)", 1000, " ms"),
        ("measured_fps", "Measured FPS (native)", 1, ""),
        ("browser_fps", "Measured FPS (browser)", 1, ""),
        ("browser_frame_time_mean_ms", "Frame Time Mean (browser)", 1, " ms"),
        ("dropped_frames", "Dropped Frames (native)", 1, ""),
        ("browser_dropped_frames", "Dropped Frames (browser)", 1, ""),
    ]

    archs = sorted(arch_data.keys())

    for metric_key, metric_label, scale, unit in metrics:
        print(f"\n{metric_label}:")
        for arch in archs:
            values = arch_data[arch].get(metric_key, [])
            print(f"  {arch:<20} {fmt_mean_sd(values, scale, unit):<25} (n={len(values)})")

    # -----------------------------------------------------------------------
    # Message delivery verification (Concern 1)
    # -----------------------------------------------------------------------
    print(f"\n{'='*90}")
    print("  MESSAGE DELIVERY VERIFICATION")
    print(f"{'='*90}")
    for arch in archs:
        pub = arch_data[arch].get("messages_published", [])
        rcv = arch_data[arch].get("messages_received", [])
        if pub and rcv:
            avg_pub = statistics.mean(pub)
            avg_rcv = statistics.mean(rcv)
            delivery_pct = (avg_rcv / avg_pub * 100) if avg_pub > 0 else 0
            print(f"  {arch:<20} Published: {avg_pub:.0f}  Received: {avg_rcv:.0f}  "
                  f"Delivery: {delivery_pct:.1f}%")

    # -----------------------------------------------------------------------
    # Welch's t-test: native vs node total memory (PSS)
    # -----------------------------------------------------------------------
    print(f"\n{'='*90}")
    print("  STATISTICAL TESTS")
    print(f"{'='*90}")

    if "native" in arch_data and "node" in arch_data:
        native_pss = [v / 1024 for v in arch_data["native"].get("pss_kb", [])]
        node_pss = [v / 1024 for v in arch_data["node"].get("pss_kb", [])]
        if native_pss and node_pss:
            t, df, p = welch_t_test(native_pss, node_pss)
            print(f"\n  Native vs Node.js — Total PSS (MB):")
            print(f"    Native: {statistics.mean(native_pss):.1f} ± {statistics.stdev(native_pss) if len(native_pss)>1 else 0:.1f}")
            print(f"    Node:   {statistics.mean(node_pss):.1f} ± {statistics.stdev(node_pss) if len(node_pss)>1 else 0:.1f}")
            if t is not None:
                print(f"    Welch's t({df:.1f}) = {t:.2f}, p = {p:.6f}")

    # -----------------------------------------------------------------------
    # Write CSV
    # -----------------------------------------------------------------------
    csv_path = os.path.join(log_dir, "aggregated_results.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        header = ["architecture", "n_trials"]
        for metric_key, metric_label, _, _ in metrics:
            header.extend([f"{metric_key}_mean", f"{metric_key}_sd"])
        writer.writerow(header)

        for arch in archs:
            row = [arch]
            # Find max n across metrics
            max_n = max(len(arch_data[arch].get(m[0], [])) for m in metrics) if metrics else 0
            row.append(max_n)
            for metric_key, _, _, _ in metrics:
                values = arch_data[arch].get(metric_key, [])
                if values:
                    row.append(f"{statistics.mean(values):.2f}")
                    row.append(f"{statistics.stdev(values):.2f}" if len(values) > 1 else "0")
                else:
                    row.extend(["", ""])
            writer.writerow(row)

    print(f"\n  CSV written: {csv_path}")
    print()


if __name__ == "__main__":
    main()
