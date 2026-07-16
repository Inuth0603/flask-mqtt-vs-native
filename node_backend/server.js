const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const mqtt = require('mqtt');
const path = require('path');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// Serve static HTML client
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

// Connect to MQTT Broker
const mqttClient = mqtt.connect('mqtt://localhost:1883');
const TOPIC = 'test/topic';

mqttClient.on('connect', () => {
  console.log('Node.js Backend Connected to MQTT Broker');
  mqttClient.subscribe(TOPIC, (err) => {
    if (err) console.error('Subscription error:', err);
  });
});

// Broadcast high-frequency MQTT packets to all connected WebSockets
mqttClient.on('message', (topic, message) => {
  const payload = message.toString();
  // We use standard 'ws' which is significantly faster than Socket.IO
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  });
});

const PORT = 5001; // Run on 5001 to avoid conflicting with Flask
server.listen(PORT, () => {
  console.log(`Optimized Node.js Full-Stack Web Router running on http://localhost:${PORT}`);
});
