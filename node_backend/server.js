const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const mqtt = require('mqtt');
const path = require('path');
const fs = require('fs');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// Serve static HTML client
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

// ---------------------------------------------------------------------------
// Connect to MQTT Broker
// ---------------------------------------------------------------------------
const mqttClient = mqtt.connect('mqtt://localhost:1883');
const TOPIC = 'test/topic';

// ---------------------------------------------------------------------------
// Instrumentation: message counter & latency  (Concerns 1 & 4)
// ---------------------------------------------------------------------------
let msgCount = 0;
const latencySamples = [];  // microseconds

/**
 * Parse the monotonic-ns timestamp embedded by the revised publisher.
 * Payload format: "<monotonic_ns>|Data Packet #<n>..."
 *
 * IMPORTANT: process.hrtime.bigint() and Python time.monotonic_ns() both use
 * CLOCK_MONOTONIC on Linux, so they are directly comparable on the same host.
 */
function computeLatencyUs(payload) {
  const sep = payload.indexOf('|');
  if (sep === -1) return -1;
  const pubNs = BigInt(payload.substring(0, sep));
  const nowNs = process.hrtime.bigint();
  if (nowNs < pubNs) return 0;  // guard
  return Number(nowNs - pubNs) / 1000;  // → microseconds
}

mqttClient.on('connect', () => {
  console.log('Node.js Backend Connected to MQTT Broker');
  mqttClient.subscribe(TOPIC, (err) => {
    if (err) console.error('Subscription error:', err);
  });
});

// Broadcast high-frequency MQTT packets to all connected WebSockets
mqttClient.on('message', (topic, message) => {
  const payload = message.toString();
  msgCount++;

  // --- Concern 4: routing-layer latency measurement ---
  const lat = computeLatencyUs(payload);
  if (lat >= 0) latencySamples.push(lat);

  // We use standard 'ws' which is significantly faster than Socket.IO
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  });
});

// ---------------------------------------------------------------------------
// Browser-side benchmark stats receiver  (Concerns 4 & 5)
// ---------------------------------------------------------------------------
let browserStats = null;

wss.on('connection', (ws) => {
  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === 'benchmark_stats') {
        browserStats = msg;
        const logDir = process.env.BENCHMARK_LOG_DIR || 'benchmark_logs';
        fs.mkdirSync(logDir, { recursive: true });
        const statsPath = path.join(logDir, 'browser_stats.json');
        fs.writeFileSync(statsPath, JSON.stringify(msg, null, 2));
        console.log(`[benchmark] Browser stats written to ${statsPath}`);
      }
    } catch (e) {
      // Not a JSON stats message — ignore (could be a regular text payload)
    }
  });
});

// ---------------------------------------------------------------------------
// Dump stats on shutdown  (Concerns 1, 4, 5)
// ---------------------------------------------------------------------------
function dumpStats() {
  console.log('\n=== Node.js Backend Stats ===');
  console.log(`Total messages received: ${msgCount}`);

  if (latencySamples.length > 0) {
    const sorted = [...latencySamples].sort((a, b) => a - b);
    const n = sorted.length;
    const median = sorted[Math.floor(n / 2)];
    const p95 = sorted[Math.floor(n * 0.95)];
    const p99 = sorted[Math.floor(n * 0.99)];
    const avg = sorted.reduce((a, b) => a + b, 0) / n;

    console.log(`Latency samples:         ${n}`);
    console.log(`  Mean:   ${(avg / 1000).toFixed(3)} ms`);
    console.log(`  Median: ${(median / 1000).toFixed(3)} ms`);
    console.log(`  p95:    ${(p95 / 1000).toFixed(3)} ms`);
    console.log(`  p99:    ${(p99 / 1000).toFixed(3)} ms`);

    // Write latency CSV
    const logDir = process.env.BENCHMARK_LOG_DIR || 'benchmark_logs';
    fs.mkdirSync(logDir, { recursive: true });

    const csvPath = path.join(logDir, 'node_latency.csv');
    const csvContent = 'latency_us\n' + sorted.map(v => v.toFixed(2)).join('\n') + '\n';
    fs.writeFileSync(csvPath, csvContent);
    console.log(`Latency log written to ${csvPath}`);

    // Write summary
    const summaryPath = path.join(logDir, 'node_summary.log');
    const summaryLines = [
      `messages_received=${msgCount}`,
      `routing_latency_median_us=${median.toFixed(2)}`,
      `routing_latency_p95_us=${p95.toFixed(2)}`,
      `routing_latency_p99_us=${p99.toFixed(2)}`,
    ];

    // Include browser-reported display latency if available
    if (browserStats && browserStats.display_latency_median_us) {
      summaryLines.push(`display_latency_median_us=${browserStats.display_latency_median_us.toFixed(2)}`);
      summaryLines.push(`display_latency_p95_us=${browserStats.display_latency_p95_us.toFixed(2)}`);
      summaryLines.push(`display_latency_p99_us=${browserStats.display_latency_p99_us.toFixed(2)}`);
      summaryLines.push(`frontend_messages_received=${browserStats.frontend_messages_received}`);
      summaryLines.push(`browser_fps=${browserStats.last_fps}`);
      if (browserStats.frame_time_mean_ms) {
        summaryLines.push(`frame_time_mean_ms=${browserStats.frame_time_mean_ms.toFixed(3)}`);
        summaryLines.push(`frame_time_max_ms=${browserStats.frame_time_max_ms.toFixed(3)}`);
        summaryLines.push(`dropped_frames=${browserStats.dropped_frames}`);
      }
    }
    summaryLines.push('');
    fs.writeFileSync(summaryPath, summaryLines.join('\n'));
  }
}

process.on('SIGINT', () => {
  dumpStats();
  process.exit(0);
});
process.on('SIGTERM', () => {
  dumpStats();
  process.exit(0);
});

const PORT = 5001; // Run on 5001 to avoid conflicting with Flask
server.listen(PORT, () => {
  console.log(`Optimized Node.js Full-Stack Web Router running on http://localhost:${PORT}`);
});
