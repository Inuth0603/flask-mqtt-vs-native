#!/usr/bin/env python3
"""
analyze_latency.py — Post-process latency logs  (Concern 4)

Reads latency CSV files produced by the instrumented backends and computes
summary statistics (mean, median, p95, p99, max) for each architecture.

Usage:
    python benchmarks/analyze_latency.py [--log-dir benchmark_logs]

Expects files like:
    benchmark_logs/native_latency.csv
    benchmark_logs/node_latency.csv
    benchmark_logs/flask_latency.csv

Output:
    Printed table + benchmark_logs/latency_comparison.csv
"""
import argparse
import csv
import os
import statistics
import sys


def parse_args():
    parser = argparse.ArgumentParser(description="Latency analysis")
    parser.add_argument(
        "--log-dir", default="benchmark_logs",
        help="Directory containing *_latency.csv files"
    )
    return parser.parse_args()


def load_latency_csv(path):
    """Load latency samples from a CSV file (header: latency_us)."""
    samples = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                samples.append(float(row["latency_us"]))
            except (ValueError, KeyError):
                continue
    return samples


def compute_stats(samples):
    """Return dict of latency statistics in milliseconds."""
    if not samples:
        return {}
    s = sorted(samples)
    n = len(s)
    return {
        "count": n,
        "mean_ms": statistics.mean(s) / 1000,
        "median_ms": s[n // 2] / 1000,
        "p95_ms": s[int(n * 0.95)] / 1000,
        "p99_ms": s[int(n * 0.99)] / 1000,
        "max_ms": s[-1] / 1000,
        "min_ms": s[0] / 1000,
    }


def main():
    args = parse_args()
    log_dir = args.log_dir

    architectures = {
        "Native C++ (GTK4)": "native_latency.csv",
        "Node.js Full-Stack": "node_latency.csv",
        "Flask (Python)": "flask_latency.csv",
    }

    results = {}
    for name, filename in architectures.items():
        path = os.path.join(log_dir, filename)
        if os.path.exists(path):
            samples = load_latency_csv(path)
            stats = compute_stats(samples)
            results[name] = stats
            print(f"\n{name} ({len(samples)} samples)")
            print(f"  Mean:   {stats['mean_ms']:.3f} ms")
            print(f"  Median: {stats['median_ms']:.3f} ms")
            print(f"  p95:    {stats['p95_ms']:.3f} ms")
            print(f"  p99:    {stats['p99_ms']:.3f} ms")
            print(f"  Max:    {stats['max_ms']:.3f} ms")
        else:
            print(f"\n{name}: {filename} not found — skipping")

    if not results:
        print("\nNo latency files found. Run the benchmarks first.")
        sys.exit(1)

    # Write comparison CSV
    out_path = os.path.join(log_dir, "latency_comparison.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "architecture", "count", "mean_ms", "median_ms",
            "p95_ms", "p99_ms", "max_ms", "min_ms"
        ])
        for name, stats in results.items():
            writer.writerow([
                name,
                stats.get("count", 0),
                f"{stats.get('mean_ms', 0):.3f}",
                f"{stats.get('median_ms', 0):.3f}",
                f"{stats.get('p95_ms', 0):.3f}",
                f"{stats.get('p99_ms', 0):.3f}",
                f"{stats.get('max_ms', 0):.3f}",
                f"{stats.get('min_ms', 0):.3f}",
            ])

    print(f"\nComparison table written to {out_path}")

    # Print formatted comparison table
    print(f"\n{'='*80}")
    print(f"  End-to-End Latency Comparison (Publish → Backend Receive)")
    print(f"{'='*80}")
    print(f"{'Architecture':<25} {'Count':>8} {'Mean':>10} {'Median':>10} {'p95':>10} {'p99':>10} {'Max':>10}")
    print("-" * 85)
    for name, stats in results.items():
        print(
            f"{name:<25} "
            f"{stats['count']:>8} "
            f"{stats['mean_ms']:>9.3f}ms "
            f"{stats['median_ms']:>9.3f}ms "
            f"{stats['p95_ms']:>9.3f}ms "
            f"{stats['p99_ms']:>9.3f}ms "
            f"{stats['max_ms']:>9.3f}ms"
        )


if __name__ == "__main__":
    main()
