import paho.mqtt.client as mqtt
import time
import sys

BROKER_ADDRESS = "localhost"
TOPIC = "test/topic"
TARGET_RATE = 5000  # msgs/sec
INTERVAL = 1.0 / TARGET_RATE

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to broker!")
    else:
        print("Failed to connect, return code %d\n", rc)

client = mqtt.Client()
client.on_connect = on_connect

print(f"Connecting to {BROKER_ADDRESS}...")
try:
    client.connect(BROKER_ADDRESS, 1883, 60)
except Exception as e:
    print(f"Error connecting: {e}")
    sys.exit(1)

client.loop_start()

print(f"Starting data flood at target {TARGET_RATE} msg/sec... Press Ctrl+C to stop.")

packet_num = 0
start_time = time.perf_counter()
next_time = time.perf_counter()
try:
    while True:
        packet_num += 1
        payload = f"Data Packet #{packet_num} | Time: {time.time():.4f}"
        client.publish(TOPIC, payload, qos=0)
        
        next_time += INTERVAL
        sleep_time = next_time - time.perf_counter()
        if sleep_time > 0:
            time.sleep(sleep_time)
except KeyboardInterrupt:
    print("\nStopping data flood.")
finally:
    elapsed_time = time.perf_counter() - start_time
    actual_rate = packet_num / elapsed_time if elapsed_time > 0 else 0
    print(f"--- Benchmark Results ---")
    print(f"Total messages published: {packet_num}")
    print(f"Total elapsed time: {elapsed_time:.2f} seconds")
    print(f"Achieved rate: {actual_rate:.2f} msg/sec")
    client.loop_stop()
    client.disconnect()
